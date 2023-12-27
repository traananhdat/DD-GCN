import math

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
import torch.nn.functional as F
from torch import cosine_similarity

def import_class(name):
    components = name.split('.')
    mod = __import__(components[0])
    for comp in components[1:]:
        mod = getattr(mod, comp)
    return mod


def conv_branch_init(conv, branches):
    weight = conv.weight
    n = weight.size(0)
    k1 = weight.size(1)
    k2 = weight.size(2)
    nn.init.normal_(weight, 0, math.sqrt(2. / (n * k1 * k2 * branches)))
    nn.init.constant_(conv.bias, 0)


def conv_init(conv):
    nn.init.kaiming_normal_(conv.weight, mode='fan_out')
    nn.init.constant_(conv.bias, 0)


def bn_init(bn, scale):
    nn.init.constant_(bn.weight, scale)
    nn.init.constant_(bn.bias, 0)


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class GTC(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=9, stride=1,group=8):
        super(GTC, self).__init__()
        pad = 4 # int((kernel_size - 1) / 2)
        self.bn1 = nn.GroupNorm(num_groups=group, num_channels=in_channels)
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=(kernel_size, 1), padding=(pad, 0),
                              stride=(stride, 1), groups=group)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU()
        conv_init(self.conv)
        bn_init(self.bn1, 1)
        bn_init(self.bn, 1)
    def forward(self, x):
        x = self.bn(self.conv(self.bn1(x)))
        return x


class CTRGC(nn.Module):
    def __init__(self, in_channels, out_channels, rel_reduction=8, mid_reduction=1):
        super(CTRGC, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        if in_channels == 3 or in_channels == 9:
            self.rel_channels = 8
            self.mid_channels = 16
        else:
            self.rel_channels = in_channels // rel_reduction
            self.mid_channels = in_channels // mid_reduction
        self.conv1 = nn.Conv2d(self.in_channels, self.rel_channels, kernel_size=1)
        self.conv2 = nn.Conv2d(self.in_channels, self.rel_channels, kernel_size=1)
        self.conv3 = nn.Conv2d(self.in_channels, self.out_channels, kernel_size=1)
        self.conv4 = nn.Conv2d(self.rel_channels, self.out_channels, kernel_size=1)
        self.tanh = nn.Tanh()
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                conv_init(m)
            elif isinstance(m, nn.BatchNorm2d):
                bn_init(m, 1)

    def forward(self, x, A=None, alpha=1):
        x1, x2, x3 = self.conv1(x).mean(-2), self.conv2(x).mean(-2), self.conv3(x)
        x1 = self.tanh(x1.unsqueeze(-1) - x2.unsqueeze(-2))
        x1 = self.conv4(x1) * alpha + (A.unsqueeze(0).unsqueeze(0) if A is not None else 0)  # N,C,V,V
        x1 = torch.einsum('ncuv,nctv->nctu', x1, x3)
        return x1


class CAGC(nn.Module):
    def __init__(self, in_channels, out_channels, A, coff_embedding=4, adaptive=True, residual=True):
        """
        todo: add parameters
        """
        super(CAGC, self).__init__()
        inter_channels = out_channels // coff_embedding
        self.inter_c = inter_channels
        self.out_c = out_channels
        self.in_c = in_channels
        self.adaptive = adaptive
        self.num_subset = A.shape[0]
        self.convs = nn.ModuleList()
        for i in range(self.num_subset):
            self.convs.append(CTRGC(in_channels, out_channels))

        if residual:
            if in_channels != out_channels:
                self.down = nn.Sequential(
                    nn.Conv2d(in_channels, out_channels, 1),
                    nn.BatchNorm2d(out_channels)
                )
            else:
                self.down = lambda x: x
        else:
            self.down = lambda x: 0
        if self.adaptive:
            self.PA = nn.Parameter(torch.from_numpy(A.astype(np.float32)))
        else:
            self.A = Variable(torch.from_numpy(A.astype(np.float32)), requires_grad=False)
        self.alpha = nn.Parameter(torch.zeros(1))
        self.bn = nn.BatchNorm2d(out_channels)
        self.soft = nn.Softmax(-2)
        self.relu = nn.ReLU(inplace=True)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                conv_init(m)
            elif isinstance(m, nn.BatchNorm2d):
                bn_init(m, 1)
        bn_init(self.bn, 1e-6)

    def forward(self, x):
        y = None
        if self.adaptive:
            A = self.PA
        else:
            A = self.A.cuda(x.get_device())
        for i in range(self.num_subset):
            z = self.convs[i](x, A[i], self.alpha)
            y = z + y if y is not None else z
        y = self.bn(y)
        y += self.down(x)
        y = self.relu(y)
        return y

# todo:注意不能整除的情况  assert
def subDDG(x, window_size): # window_size避免混淆还是换成列表
    B, H, W, C = x.shape
    # x = x.view(B, H // window_size, window_size, W // window_size, window_size, C)
    x = x.view(B, H // window_size[0], window_size[0], W // window_size[1], window_size[1], C)
    windows = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size[0], window_size[1], C)
    return windows
# todo: TCN会导致维度变化，窗口改成均分


class subDDG_Attention(nn.Module):  # 单个窗口的attention
    r""" Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.

    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """
    def __init__(self, in_channel, window_size, num_heads, red=4, qkv_bias=True, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.in_channel = in_channel
        self.window_size = window_size  # Wh, Ww
        # self.num_heads = num_heads
        if self.in_channel == 3:
            self.num_heads = 3
            self.red = 1
        else:
            self.num_heads = num_heads
            self.red = red
        self.red_channel = self.in_channel//self.red
        self.proj1 = nn.Linear(self.in_channel, self.red_channel)
        head_dim = self.red_channel // self.num_heads
        self.scale = qk_scale or head_dim ** -0.5  # 优先取qk_scale的值，若为None则是head_dim ** -0.5

        # define a parameter table of relative position bias
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1), self.num_heads))  # 2*Wh-1 * 2*Ww-1, nH
        # todo：是否需要相对位置编码？是因为划分了patch，我需要吗？
        # get pair-wise relative position index for each token inside the window
        coords_h = torch.arange(self.window_size[0])  # [0, 1, 2, 3, 4, 5..., h]
        coords_w = torch.arange(self.window_size[1])  # [0, 1, 2, 3, 4, 5..., w]
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
        # [:, :, None]在原有维度的前面增加一维，这里会出现负数
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2
        relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0 消除了负数 可以理解为x轴的相对位置
        relative_coords[:, :, 1] += self.window_size[1] - 1  # y轴的相对位置
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1  # 将二维的相对位置映射为一维的，相当于将二维张量展平后的相对位置
        # 相对位置编码 表示窗口内第i个patch相对于第j个patch的坐标。窗口内共有h*w个patch, 例：x方向的取值范围是[-h,h]
        relative_position_index = relative_coords.sum(-1)  # Wh*Ww, Wh*Ww  都大于0
        # print(relative_position_index)
        self.register_buffer("relative_position_index", relative_position_index)

        self.qkv = nn.Linear(self.red_channel, self.red_channel * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj2 = nn.Linear(self.red_channel, self.in_channel)
        self.proj_drop = nn.Dropout(proj_drop)

        trunc_normal_(self.relative_position_bias_table, std=.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, mask=None):
        """
        Args:
            x: input features with shape of (num_windows*B, N, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # x = x.permute(0, 2, 1)
        x = self.proj1(x)
        # x = x.permute(0, 2, 1)
        B_, L, C = x.shape
        qkv = self.qkv(x).reshape(B_, L, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # make torchscript happy (cannot use tensor as tuple)

        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))
        relative_position_bias = self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)  # Wh*Ww,Wh*Ww,nH
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww
        attn = attn + relative_position_bias.unsqueeze(0)

        # if mask is not None:
        #     nW = mask.shape[0]
        #     attn = attn.view(B_ // nW, nW, self.num_heads, L, L) + mask.unsqueeze(1).unsqueeze(0)
        #     attn = attn.view(-1, self.num_heads, L, L)
        #     attn = self.softmax(attn)
        # else:
        #     attn = self.softmax(attn)
        attn = self.softmax(attn)

        attn = self.attn_drop(attn)  # dropout之后的

        x = (attn @ v).transpose(1, 2).reshape(B_, L, C)
        x = self.proj2(x)    # 投影层，保持维度不变
        x = self.proj_drop(x)
        return x


class STSE_Encoder(nn.Module):  # 整体的window  todo: note:先只考虑单列window
    def __init__(self, in_channel, window_size, num_heads,red=4,qkv_bias=True, qk_scale=None, attn_drop=0., proj_drop=0., drop_path=0.):
        super(STSE_Encoder, self).__init__()
        self.in_channel = in_channel
        self.window_size = window_size
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.attn = subDDG_Attention(in_channel, self.window_size, num_heads=num_heads, red = red,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=proj_drop)
        self.bn1 = nn.BatchNorm2d(in_channel)
        self.bn2 = nn.BatchNorm2d(in_channel)
        self.mlp = Mlp(in_channel)
        self.relu = nn.ReLU()
        bn_init(self.bn1, 1)
        bn_init(self.bn2, 1)
        # self.norm1 = norm_layer(in_channel)
        # self.norm2 = norm_layer(in_channel)
    def forward(self, x):  # todo:输入是什么？ n*m,c,t,v
        # N,C,T,V = x.shape()
        N,C,T,V = x.size()
        # assert self.in_channel == C, "window_size has wrong size"
        x = self.bn1(x)
        x = x.permute(0, 2, 3, 1).contiguous()
        assert self.window_size[1] == V, "window_size has wrong size"

        win_num = T//self.window_size[0]
        shortcut = x  # TODO:添加跳跃连接
        x_windows = subDDG(x, self.window_size)  # 返回窗口个数和每个窗口的大小 window_size是列表 todo:shifted
        x_windows = x_windows.view(-1, self.window_size[0] * self.window_size[1], C)  # nW*B, window_size*window_size, C
        # W-MSA/SW-MSA
        # attn_windows = self.attn(x_windows, mask=self.attn_mask)  # nW*B, window_size*window_size, C
        attn_windows = self.attn(x_windows)  # nW*B, window_size*window_size, C
        # todo: mask什么作用？是否需要？
        # merge windows
        attn_windows = attn_windows.view(-1, win_num, self.window_size[0], self.window_size[1], C)
        former_attn = attn_windows.view(-1, win_num*self.window_size[0], self.window_size[1], C)
        # todo:需要分步？需要保证时间顺序没有变

        x = shortcut + self.drop_path(former_attn)
        # FFN
        x = x + self.drop_path(self.mlp(x))
        x = x.permute(0, 3, 1, 2).contiguous()
        return self.relu(x)  # N, C, T, V


class STSE_Encoder_part(nn.Module):  # 整体的window  todo: note:先只考虑单列window
    def __init__(self, in_channel, window_size, num_heads,red=4,qkv_bias=True, qk_scale=None, attn_drop=0., proj_drop=0., drop_path=0.):
        super(STSE_Encoder_part, self).__init__()
        self.in_channel = in_channel
        self.window_size = window_size
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.attn = subDDG_Attention(in_channel, self.window_size, num_heads=num_heads, red = red,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=proj_drop)
        self.bn1 = nn.BatchNorm2d(in_channel)
        self.bn2 = nn.BatchNorm2d(in_channel)
        self.mlp = Mlp(in_channel)
        self.relu = nn.ReLU()
        bn_init(self.bn1, 1)
        bn_init(self.bn2, 1)
        # self.norm1 = norm_layer(in_channel)
        # self.norm2 = norm_layer(in_channel)
    def forward(self, x):  # todo:输入是什么？ n*m,c,t,v
        # N,C,T,V = x.shape()
        N,C,T,V = x.size()
        # assert self.in_channel == C, "window_size has wrong size"
        x = self.bn1(x)
        x = x.permute(0, 2, 3, 1).contiguous()
        assert self.window_size[0] == T, "window_size has wrong size"

        win_num = V//self.window_size[1]
        shortcut = x  # TODO:添加跳跃连接
        x_windows = subDDG(x, self.window_size)  # 返回窗口个数和每个窗口的大小 window_size是列表 todo:shifted
        x_windows = x_windows.view(-1, self.window_size[0] * self.window_size[1], C)  # nW*B, window_size*window_size, C
        # W-MSA/SW-MSA
        # attn_windows = self.attn(x_windows, mask=self.attn_mask)  # nW*B, window_size*window_size, C
        attn_windows = self.attn(x_windows)  # nW*B, window_size*window_size, C
        # todo: mask什么作用？是否需要？
        # merge windows
        attn_windows = attn_windows.view(-1, win_num, self.window_size[0], self.window_size[1], C)
        former_attn = attn_windows.permute(0, 2, 1, 3, 4).contiguous().view(-1, self.window_size[0], win_num*self.window_size[1], C)
        # todo:需要分步？需要保证时间顺序没有变
        x = shortcut + self.drop_path(former_attn)
        # FFN
        x = x + self.drop_path(self.mlp(x))
        x = x.permute(0, 3, 1, 2).contiguous()
        return self.relu(x)


class Pool_Channel(nn.Module):
    def __init__(self, kernel_size, stride=1):
        super(Pool_Channel, self).__init__()
        self.max_pooling = nn.MaxPool2d(
            kernel_size=kernel_size,
            stride=stride
        )
    def forward(self, x):
        x = x.transpose(1, 3)
        x = self.max_pooling(x)
        out = x.transpose(1, 3)
        return out


class SAGC_pre(nn.Module):
    def __init__(self, in_channels, out_channels, A, stride=1, residual=True, window_size=[4, 25], num_heads=4, group=8):
        super(SAGC_pre, self).__init__()

        self.cagc = CAGC(in_channels, out_channels, A)
        self.stse_encoder = STSE_Encoder(out_channels, window_size, num_heads)
        #self.stse_encoder2 = STSE_Encoder(out_channels, window_size2, num_heads)
        #self.pool_c = nn.Pool_Channel(1)
        #self.pool_t = nn.AvgPool2d((Pool_size[0], 1))
        #self.pool_v = nn.AvgPool2d((1, Pool_size[1]))
        #self.fc = nn.Linear(in_channels*2, out_channels)
        self.stse_gtc = GTC(out_channels, out_channels, stride=stride, group=group)
        self.relu = nn.ReLU()
        if not residual:
            self.residual = lambda x: 0
        else:
            self.residual = lambda x: x
    def forward(self, x):
        x = self.cagc(x)
        x1 = self.stse_encoder(x)
        x2 = self.stse_gtc(x1) + self.residual(x)
        return self.relu(x2)


class SE_Block(nn.Module):
    def __init__(self, ch_in, reduction=4):
        super(SE_Block, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)  # 全局自适应池化
        self.fc = nn.Sequential(
            nn.Linear(ch_in, ch_in // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(ch_in // reduction, ch_in, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)  # squeeze操作
        y = self.fc(y).view(b, c, 1, 1)  # FC获取通道注意力权重，是具有全局信息的
        return x * y.expand_as(x)  # 注意力作用每一个通道上


class SAGC(nn.Module):
    def __init__(self, in_channels, out_channels, A, stride=1, residual=True, window_size1=[4, 25],window_size2=[64, 5], num_heads=4, group=8):  # pool_size=[64, 25], dim=128
        super(SAGC, self).__init__()

        self.cagc = CAGC(in_channels, out_channels, A)
        self.stse_encoder = STSE_Encoder(out_channels, window_size1, num_heads)
        self.stse_encoder2 = STSE_Encoder_part(out_channels, window_size2, num_heads)
        # self.pool = nn.AvgPool2d(out_channels, )
        self.fc = nn.Linear(in_features=out_channels*2, out_features=out_channels)
        self.stse_gtc = GTC(out_channels, out_channels, stride=stride, group=group)
        self.relu = nn.ReLU()
        if not residual:
            self.residual = lambda x: 0
        else:
            self.residual = lambda x: x
    def forward(self, x):
        x = self.cagc(x)
        x1 = self.stse_encoder(x)
        x2 = self.stse_encoder2(x)
        x_cat = torch.cat((x1, x2), dim=1).permute(0, 2, 3, 1)
        x_f = self.fc(x_cat).permute(0, 3, 1, 2).contiguous()
        x = self.stse_gtc(x_f) + self.residual(x)
        return self.relu(x)

class SAGC_CMD(nn.Module):
    def __init__(self, in_channels, out_channels, A, stride=1, residual=True, window_size1=[4, 25],window_size2=[64, 5], num_heads=4, group=8):  # pool_size=[64, 25], dim=128
        super(SAGC_CMD, self).__init__()
        self.num_class = 60
        self.M= 2
        self.cagc = CAGC(in_channels, out_channels, A)
        self.stse_encoder = STSE_Encoder(out_channels, window_size1, num_heads)
        self.stse_encoder2 = STSE_Encoder_part(out_channels, window_size2, num_heads)
        # self.pool = nn.AvgPool2d(out_channels, )
        self.pro = nn.Linear(in_features=out_channels*2, out_features=self.num_class)
        self.fc = nn.Linear(in_features=out_channels*2, out_features=out_channels)
        self.stse_gtc = GTC(out_channels, out_channels, stride=stride, group=group)
        self.relu = nn.ReLU()
        if not residual:
            self.residual = lambda x: 0
        else:
            self.residual = lambda x: x
    def forward(self, x):
        x = self.cagc(x)
        x1 = self.stse_encoder(x)
        x2 = self.stse_encoder2(x)
        x_cat = torch.cat((x1, x2), dim=1).permute(0, 2, 3, 1)
        x_f = self.fc(x_cat).permute(0, 3, 1, 2).contiguous()
        x = self.stse_gtc(x_f) + self.residual(x)
        return self.relu(x), x1, x2


class SAGC_SE_ADD(nn.Module):
    def __init__(self, in_channels, out_channels, A, stride=1, residual=True, window_size1=[4, 25],window_size2=[64, 1], num_heads=4, group=8):  # pool_size=[64, 25], dim=128
        super(SAGC_SE_ADD, self).__init__()
        self.cagc = CAGC(in_channels, out_channels, A)
        self.stse_encoder = STSE_Encoder(out_channels, window_size1, num_heads)
        self.stse_encoder2 = STSE_Encoder_part(out_channels, window_size2, num_heads)
        # self.alpha = torch.nn.Parameter(torch.FloatTensor(1), requires_grad=True)
        self.stse_gtc = GTC(out_channels, out_channels, stride=stride, group=group)
        self.relu = nn.ReLU()
        self.se1 = SE_Block(out_channels)
        self.se2 = SE_Block(out_channels)
        if not residual:
            self.residual = lambda x: 0
        else:
            self.residual = lambda x: x
    def forward(self, x):
        x = self.cagc(x)
        x1 = self.se1(self.stse_encoder(x))
        x2 = self.se2(self.stse_encoder2(x))
        x = self.stse_gtc(x1 + x2) + self.residual(x)
        return self.relu(x)

# ###CMD
# def _momentum_update_key_encoder(self):
#     """
#     Momentum update of the key encoder
#     """
#     for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
#         param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)
#
#  @torch.no_grad()
# def _momentum_update_key_encoder_motion(self):
#     for param_q, param_k in zip(self.encoder_q_motion.parameters(), self.encoder_k_motion.parameters()):
#         param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)
class SAGC_SE_ADD_3w(nn.Module):
    def __init__(self, in_channels, out_channels, A, stride=1, residual=True, window_size1=[4, 25],window_size2=[64, 1], window_size3=[1, 25], num_heads=4, group=8):  # pool_size=[64, 25], dim=128
        super(SAGC_SE_ADD, self).__init__()
        self.cagc = CAGC(in_channels, out_channels, A)
        self.stse_encoder = STSE_Encoder(out_channels, window_size1, num_heads)
        self.stse_t = STSE_Encoder_part(out_channels, window_size2, num_heads)
        self.stse_s = STSE_Encoder_part(out_channels, window_size3, num_heads)
        # self.pool = nn.AvgPool2d(out_channels, )
        self.stse_gtc = GTC(out_channels, out_channels, stride=stride, group=group)
        self.relu = nn.ReLU()
        self.se1 = SE_Block(out_channels)
        self.se2 = SE_Block(out_channels)
        if not residual:
            self.residual = lambda x: 0
        else:
            self.residual = lambda x: x
    def forward(self, x):
        x = self.cagc(x)
        x1 = self.se1(self.stse_s(x))
        x2 = self.se2(self.stse_t(x))
        x = self.stse_gtc(self.stse_encoder(x1+x2))+self.residual(x)
        return self.relu(x)

@torch.no_grad()
def _dequeue_and_enqueue(self, keys):
    batch_size = keys.shape[0]
    ptr = int(self.queue_ptr)
    self.queue[:, ptr:ptr + batch_size] = keys.T
    ptr = (ptr + batch_size) % self.K  # move pointer
    self.queue_ptr[0] = ptr


class Model(nn.Module):
    def __init__(self, num_class=60, num_point=25, num_person=2, graph=None, graph_args=dict(), in_channels=3, window_size=[4, 25],num_heads=3):
        """
        todo: add parameters:
        """
        super(Model, self).__init__()

        if graph is None:
            raise ValueError()
        else:
            Graph = import_class(graph)
            self.graph = Graph(**graph_args)

        A = self.graph.A
        self.data_bn = nn.BatchNorm1d(num_person * in_channels * num_point)

        self.l1 = SAGC_SE_ADD(3, 64, A, residual=False)
        self.l2 = SAGC_SE_ADD(64, 64, A)
        self.l3 = SAGC_SE_ADD(64, 64, A)
        self.l4 = SAGC_SE_ADD(64, 64, A)
        self.l5 = SAGC_SE_ADD(64, 128, A)
        self.l6 = SAGC_SE_ADD(128, 128, A)
        self.l7 = SAGC_SE_ADD(128, 128, A)
        self.l8 = SAGC_SE_ADD(128, 256, A)
        # self.l9 = SAGC(256, 256, A)
        self.l10 = SAGC_SE_ADD(256, 256, A)

        self.fc = nn.Linear(256, num_class)
        nn.init.normal_(self.fc.weight, 0, math.sqrt(2. / num_class))
        bn_init(self.data_bn, 1)

    def forward(self, x):
        N, C, T, V, M = x.size()

        x = x.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
        x = self.data_bn(x)
        x = x.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V)

        x = self.l1(x)
        x = self.l2(x)
        x = self.l3(x)
        x = self.l4(x)
        x = self.l5(x)
        x = self.l6(x)
        x = self.l7(x)
        x = self.l8(x)
        # x = self.l9(x)
        x = self.l10(x)

        # N*M,C,T,V
        c_new = x.size(1)
        x = x.view(N, M, c_new, -1)
        x = x.mean(3).mean(1)
        return self.fc(x)



