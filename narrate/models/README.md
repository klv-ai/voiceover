# RNNoise models for ffmpeg's `arnndn`

Weight files from [GregorR/rnnoise-models](https://github.com/GregorR/rnnoise-models),
trained for Xiph's RNNoise (BSD-3-Clause).

| file | source model | measured on this footage |
|------|--------------|--------------------------|
| `mp.rnnn` | marathon-prescription-2018-08-29 | +16.0 dB SNR — default |
| `sh.rnnn` | somnolent-hogwash-2018-09-01 | +13.8 dB SNR |

Measured against a 2-minute sample of `Ask.mov`: pause-region RMS versus
speech-region RMS, before and after. For comparison, `afftdn` — the spectral
denoiser this replaced — moved the same number by **+0.2 dB**, i.e. it was
doing nothing audible while still colouring the speech.
