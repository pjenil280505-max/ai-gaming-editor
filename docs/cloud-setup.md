# Cloud environment setup (Claude Code on the web)

The Claude Code cloud container (Ubuntu 24.04, Python 3.11) does **not** ship
FFmpeg. PyYAML was already present (6.0.1) in the session that wrote this file.

Paste this into the environment's **Setup script** (cloud environment menu in
the session title bar → Edit → Setup script). New sessions run it on start.

```bash
#!/bin/bash
set -e
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ffmpeg
python3 -c "import yaml" 2>/dev/null || python3 -m pip install -q PyYAML
ffmpeg -hide_banner -version | head -1
ffprobe -hide_banner -version | head -1
```

Verified in session 1 (2026-09-26): the `apt-get` commands ran as root without
`sudo` and installed `ffmpeg 6.1.1-3ubuntu5` (ffprobe is in the same package,
libx264 / libx265 / aac encoders included). The `pip` line was not exercised
because PyYAML was already installed.

Colab ships the same FFmpeg build, `6.1.1-3ubuntu5` (owner checked with
`!ffmpeg -version | head -1` on 2026-09-26; DEC-009), so nothing needs
installing there. The unit tests have not yet been run on Colab; notebook cell
C7 (not built yet) will do that.
