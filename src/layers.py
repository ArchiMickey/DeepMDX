import torch
from torch import nn
import torch.nn.functional as F
from src import spec_utils
from src.utils import _weights_init


class Conv2DBNActiv(nn.Module):

    def __init__(self, nin, nout, ksize=3, stride=1, pad=1, dilation=1, activ=nn.ReLU, use_bn=True):
        super(Conv2DBNActiv, self).__init__()
        self.conv = nn.Conv2d(
            nin, nout,
            kernel_size=ksize,
            stride=stride,
            padding=pad,
            dilation=dilation,
            bias=False
        )
        self.use_bn = use_bn
        if self.use_bn:
            self.bn = nn.BatchNorm2d(nout)
        self.activ = activ()
    
        _weights_init(self.conv)
        if self.use_bn:
            _weights_init(self.bn)

    def __call__(self, x):
        x = self.conv(x)
        if self.use_bn:
            x = self.bn(x)
        x = self.activ(x)
        return x

class Encoder(nn.Module):

    def __init__(self, nin, nout, ksize=3, stride=1, pad=1, activ=nn.LeakyReLU, use_bn=True):
        super(Encoder, self).__init__()
        self.conv1 = Conv2DBNActiv(nin, nout, ksize, stride, pad, activ=activ, use_bn=use_bn)
        self.conv2 = Conv2DBNActiv(nout, nout, ksize, 1, pad, activ=activ, use_bn=use_bn)

    def __call__(self, x):
        h = self.conv1(x)
        h = self.conv2(h)

        return h


class Decoder(nn.Module):

    def __init__(self, nin, nout, ksize=3, stride=1, pad=1, activ=nn.ReLU, dropout=False, use_bn=True):
        super(Decoder, self).__init__()
        self.conv1 = Conv2DBNActiv(nin, nout, ksize, 1, pad, activ=activ, use_bn=use_bn)
        # self.conv2 = Conv2DBNActiv(nout, nout, ksize, 1, pad, activ=activ)
        self.dropout = nn.Dropout2d(0.1) if dropout else None

    def __call__(self, x, skip=None):
        x = F.interpolate(x, scale_factor=2, mode='bilinear', align_corners=True)

        if skip is not None:
            skip = spec_utils.crop_center(skip, x)
            x = torch.cat([x, skip], dim=1)

        h = self.conv1(x)
        # h = self.conv2(h)

        if self.dropout is not None:
            h = self.dropout(h)
        return h


class ASPPModule(nn.Module):

    def __init__(self, nin, nout, dilations=(4, 8, 12), activ=nn.ReLU, dropout=False, use_bn=True):
        super(ASPPModule, self).__init__()
        self.conv1 = nn.Sequential(
            nn.AdaptiveAvgPool2d((1, None)),
            Conv2DBNActiv(nin, nout, 1, 1, 0, activ=activ, use_bn=use_bn)
        )
        self.conv2 = Conv2DBNActiv(
            nin, nout, 1, 1, 0, activ=activ
        )
        self.conv3 = Conv2DBNActiv(
            nin, nout, 3, 1, dilations[0], dilations[0], activ=activ, use_bn=use_bn
        )
        self.conv4 = Conv2DBNActiv(
            nin, nout, 3, 1, dilations[1], dilations[1], activ=activ, use_bn=use_bn
        )
        self.conv5 = Conv2DBNActiv(
            nin, nout, 3, 1, dilations[2], dilations[2], activ=activ, use_bn=use_bn
        )
        self.bottleneck = Conv2DBNActiv(
            nout * 5, nout, 1, 1, 0, activ=activ, use_bn=use_bn
        )
        self.dropout = nn.Dropout2d(0.1) if dropout else None

    def forward(self, x):
        _, _, h, w = x.size()
        feat1 = F.interpolate(self.conv1(x), size=(h, w), mode='bilinear', align_corners=True)
        feat2 = self.conv2(x)
        feat3 = self.conv3(x)
        feat4 = self.conv4(x)
        feat5 = self.conv5(x)
        out = torch.cat((feat1, feat2, feat3, feat4, feat5), dim=1)
        out = self.bottleneck(out)

        if self.dropout is not None:
            out = self.dropout(out)

        return out


class LSTMModule(nn.Module):

    def __init__(self, nin_conv, nin_lstm, nout_lstm, activ=nn.ReLU, use_bn=True):
        super(LSTMModule, self).__init__()
        self.conv = Conv2DBNActiv(nin_conv, 1, 1, 1, 0, use_bn=use_bn)
        self.lstm = nn.LSTM(
            input_size=nin_lstm,
            hidden_size=nout_lstm // 2,
            bidirectional=True
        )
        _activ = activ(0.2) if isinstance(activ, nn.LeakyReLU) else activ()
        if use_bn:
            self.dense = nn.Sequential(
                nn.Linear(nout_lstm, nin_lstm),
                _activ,
            )
        else:
            self.dense = nn.Sequential(
                nn.Linear(nout_lstm, nin_lstm),
                nn.BatchNorm1d(nin_lstm),
                _activ,
            )

    def forward(self, x):
        N, _, nbins, nframes = x.size()
        h = self.conv(x)[:, 0]  # N, nbins, nframes
        h = h.permute(2, 0, 1)  # nframes, N, nbins
        h, _ = self.lstm(h)
        h = self.dense(h.reshape(-1, h.size()[-1]))  # nframes * N, nbins
        h = h.reshape(nframes, N, 1, nbins)
        h = h.permute(1, 2, 3, 0)

        return h
