# 🎼 TuneReader

Convert scanned or vector-based sheet music (PDF/image) into audible MP3 using Audiveris, Music21, and FluidSynth.  
TuneReader uses Optical Music Recognition (OMR) to interpret musical notation and generate audio automatically.

---

## 📌 Features

- 🖼️ Accepts PDFs and image files (JPG, PNG)
- 🧠 Uses **Audiveris** (OMR engine) to extract **MusicXML** from scanned music
- 🎵 Optional fallback to **MuseScore** for vector PDFs
- ⚙️ Parses and cleans MusicXML using `music21`
- 🎹 Converts to MIDI and then MP3 using **FluidSynth** and **FFmpeg**
- 🔊 Auto-plays the resulting MP3 on your system

---

## 📦 Requirements

### 🧰 System Dependencies (Must Be Installed Manually)

| Tool         | Purpose                            | Example Path or CLI        |
|--------------|-------------------------------------|-----------------------------|
| **Audiveris**| Optical Music Recognition (Java)   | `/opt/audiveris/bin/Audiveris` |
| **MuseScore**| Fallback MusicXML export (PDF only)| `musescore`, `mscore`, or `musescore3` |
| **FluidSynth**| Synthesizes MIDI to WAV           | `fluidsynth` CLI tool       |
| **FFmpeg**   | Converts WAV to MP3                 | `ffmpeg` CLI tool           |
| **SoundFont (.sf2)** | Required for FluidSynth    | `/usr/share/sounds/sf2/FluidR3_GM.sf2` |
| **Poppler**  | Required for PDF-to-image conversion | `poppler-utils` or `poppler` |

### Install system dependencies:

```bash
# Ubuntu/Debian
sudo apt install ffmpeg fluidsynth poppler-utils
# macOS
brew install ffmpeg fluidsynth poppler
```

### Install Dependencies
```bash
poetry install
poetry shell
```

---

## Run the Project

```bash
python main.py
```
