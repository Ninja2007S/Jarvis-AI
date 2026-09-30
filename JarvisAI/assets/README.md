# Startup video

To show a native video intro before the JARVIS HUD opens, put your video here
with this exact filename:

```text
startup_intro.mp4
```

The video's embedded audio plays through the default system output. The intro
has a **SKIP INTRO** button and also skips with **Esc**. If the file is absent,
JARVIS starts normally. A short MP4 using H.264 video and AAC audio is a good
starting format, but supported codecs depend on the operating system's Qt
Multimedia backend.
