"""
PDF/Image to MP3 Sheet Music Converter using Audiveris + Music21 + FluidSynth

This script performs Optical Music Recognition (OMR) on sheet music in PDF or image formats
(e.g., JPG, PNG) and converts them to audible MP3 output. It supports multi-page PDFs,
uses Audiveris (Java-based OMR engine), and Includes a fallback to MuseScore for vector-based (non-scanned) PDF import.

Workflow:
---------
1. Input: Single image or multi-page PDF sheet music file.
2. Conversion:
   - PDF pages are split into 400 DPI grayscale PNGs.
   - Audiveris extracts MusicXML from each image.
   - If Audiveris fails, MuseScore is tried (PDFs only).
3. MusicXML is parsed using `music21`, cleaned (bad repeats removed, tempo normalized).
4. MIDI is generated and converted to MP3 using FluidSynth and FFmpeg.
5. MP3 is auto-played after generation.

Requirements:
-------------
- Python 3.11+
- Installed CLI tools:
  - Audiveris (Java OMR tool): e.g. `/opt/audiveris/bin/Audiveris`
  - MuseScore (optional fallback): musescore, musescore3, or mscore
  - FluidSynth (MIDI synth): `fluidsynth`
  - FFmpeg (audio encoding): `ffmpeg`
- A SoundFont file: e.g., `/usr/share/sounds/sf2/FluidR3_GM.sf2`
- Python packages:
  - music21
  - pdf2image
  - natsort

Usage:
------
1. Set `input_file` and `output_dir` variables in the USER CONFIGURATION section.
2. Ensure dependencies are installed and paths are correct.
3. Run the script:
   ```bash```
   python main.py

"""

import json
import os
import subprocess
import shutil
import platform
import logging
from pathlib import Path
from music21 import converter, stream, tempo, chord, note
from natsort import natsorted
from pdf2image import convert_from_path
os.environ["TESSDATA_PREFIX"] = "/usr/share/tesseract-ocr/4.00/tessdata"
# === USER CONFIGURATION ===

# Path to the input file (can be a scanned sheet music PDF or an image like PNG/JPG)
input_file = Path("Ed Sheeran-Shape of you.pdf")  # PDF or image

# Directory where all intermediate and final outputs (images, MusicXML, MIDI, MP3) will be saved
output_dir = Path("output")

# Path to the Audiveris executable (Java-based OMR tool) — must be correctly installed and accessible
audiveris_bin = Path("/opt/audiveris/bin/Audiveris")

# Path to a General MIDI SoundFont (.sf2) file used by FluidSynth to synthesize audio from MIDI
soundfont_path = Path("/usr/share/sounds/sf2/FluidR3_GM.sf2")

# === Logging setup ===
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger()

"""
Strategy	        Keeps
top        -> Highest - pitched note
bottom     -> Lowest - pitched note
first      -> First pitch (as listed)
last       -> Last pitch (as listed)
"""
strategy= None

left_hand = False
right_hand = False

# Shift all notes by N semitones (positive = up, negative = down)
transpose_interval = 0  # Example: +2 semitones up, or -1 for down


# === Dependency check ===
def check_dependencies():
    """
        Check if all required binaries and files exist.

        Verifies:
        - Audiveris executable exists at the given path.
        - SoundFont file exists.
        - Required CLI tools are installed: `fluidsynth`, `ffmpeg`.

        Exits the script with an error log if any are missing.
        """

    for name, path in [
        ("Audiveris", audiveris_bin),
        ("SoundFont", soundfont_path)
    ]:
        if not path.exists():
            log.error(f"{name} not found: {path}")
            exit(1)
    for binary in ["fluidsynth", "ffmpeg"]:
        if shutil.which(binary) is None:
            log.error(f"{binary} not found. Please install it.")
            exit(1)

def make_score_monophonic(score: stream.Score, strategy: str) -> stream.Score:
    """
    Convert a polyphonic score to monophonic by keeping only one note per offset,
    based on the specified harmony strategy.

    strategy:
        - 'top'    → keep highest pitch
        - 'bottom' → keep lowest pitch
        - 'first'  → keep first note in chord
        - 'last'   → keep last note in chord
    """

    mono_score = stream.Score()
    flat = score.flatten().notes.stream()
    note_dict = {}

    for n in flat:
        offset = round(n.offset, 3)

        # Convert chord to note using strategy
        if isinstance(n, chord.Chord):
            if strategy == "top":
                selected = note.Note(max(n.pitches))
            elif strategy == "bottom":
                selected = note.Note(min(n.pitches))
            elif strategy == "first":
                selected = note.Note(n.pitches[0])
            elif strategy == "last":
                selected = note.Note(n.pitches[-1])
            else:
                raise ValueError(f"Unknown strategy: {strategy}")
            selected.duration = n.duration
            selected.offset = n.offset
        else:
            selected = n

        # Replace if no note yet at this offset or selected one is "preferred"
        if offset not in note_dict:
            note_dict[offset] = selected
        else:
            # Optional: decide replacement logic
            note_dict[offset] = selected  # overwrite with new strategy

    mono_part = stream.Part()
    for offset in sorted(note_dict):
        mono_note = note_dict[offset]
        mono_part.insert(mono_note.offset, mono_note)

    mono_score.insert(0, mono_part)
    return mono_score

def filter_score_by_pitch(score: stream.Score, min_pitch: int = None, max_pitch: int = None) -> stream.Score:
    """
    Filter the score to only include notes within the given pitch range (inclusive).

    Parameters:
        min_pitch (int): Minimum MIDI pitch to keep (e.g., 0 = lowest possible note)
        max_pitch (int): Maximum MIDI pitch to keep (e.g., 60 = middle C for left hand)

    Returns:
        stream.Score: A new score containing only the filtered notes.
    """

    filtered_score = stream.Score()
    part = stream.Part()

    for n in score.flatten().notes:
        if n.isNote:
            pitch_val = n.pitch.midi
            if ((min_pitch is None or pitch_val >= min_pitch) and
                    (max_pitch is None or pitch_val <= max_pitch)):
                part.insert(n.offset, n)
        elif n.isChord:
            # Filter pitches in the chord
            pitches = [p for p in n.pitches if
                       (min_pitch is None or p.midi >= min_pitch) and
                       (max_pitch is None or p.midi <= max_pitch)]
            if pitches:
                new_chord = chord.Chord(pitches)
                new_chord.duration = n.duration
                new_chord.offset = n.offset
                part.insert(new_chord.offset, new_chord)

    filtered_score.insert(0, part)
    return filtered_score

def extract_midi_note_sequence(midi_path: Path) -> list[dict]:
    """
    Extract note sequences from a MIDI file for comparison/debugging.

    Returns a list of dicts with offset and pitch info.
    """
    midi_score = converter.parse(midi_path)
    sequence = []
    for n in midi_score.recurse().notes:
        if n.isNote:
            sequence.append({
                "offset": round(n.offset, 2),
                "pitch": n.nameWithOctave
            })
        elif n.isChord:
            sequence.append({
                "offset": round(n.offset, 2),
                "pitch": n.pitches[0].nameWithOctave
            })
    return sequence


def save_note_sequence_as_json(notes: list[dict], json_path: Path):
    with open(json_path, "w") as f:
        json.dump(notes, f, indent=2)

# === Audio playback ===
def play_audio(mp3_path: Path):
    """
        Attempt to play the generated MP3 file using the system's default audio player.

        Parameters:
            mp3_path (Path): Path to the MP3 file.

        Platform-specific behavior:
        - macOS: Uses `afplay`
        - Linux: Uses `xdg-open`
        - Windows: Uses `os.startfile`
        """

    try:
        if platform.system() == "Darwin":
            subprocess.run(["afplay", str(mp3_path)])
        elif platform.system() == "Linux":
            subprocess.run(["xdg-open", str(mp3_path)])
        elif platform.system() == "Windows":
            os.startfile(str(mp3_path))
    except Exception as e:
        log.warning(f"Could not play audio: {e}")

# === Convert input to images ===
def convert_to_images(input_path: Path, temp_dir: Path) -> list[Path]:
    """
        Convert a PDF file or copy a single image into a temporary image directory.

        Parameters:
            input_path (Path): Path to input PDF or image.
            temp_dir (Path): Directory to store output images.

        Returns:
            list[Path]: List of generated or copied image paths.

        Notes:
        - PDFs are split into 400 DPI grayscale PNGs.
        - Single image files (JPG, PNG) are copied and renamed as page_001.png.
        """

    temp_dir.mkdir(parents=True, exist_ok=True)
    image_paths = []
    if input_path.suffix.lower() == ".pdf":
        log.info("Converting PDF to high-res grayscale images...")
        pages = convert_from_path(str(input_path), dpi=400)
        for i, page in enumerate(pages):
            page = page.convert("L")
            img_path = temp_dir / f"page_{i+1:03}.png"
            page.save(img_path)
            image_paths.append(img_path)
    else:
        log.info(f"Copying input image: {input_path.name}")
        img_path = temp_dir / "page_001.png"
        shutil.copy(input_path, img_path)
        image_paths.append(img_path)
    return image_paths

# === Run Audiveris ===
def run_audiveris(images: list[Path], out_dir: Path):
    """
    Run Audiveris OMR in batch mode on a list of image files to extract MusicXML.

    Parameters:
        images (list[Path]): List of image paths to process.
        out_dir (Path): Output directory for MusicXML files.

    This function logs output to a single batch log file. If Audiveris fails,
    it logs a warning and returns without falling back automatically.
    """

    log_path = out_dir / "audiveris_batch.log"
    log.info(f"Running Audiveris batch on {len(images)} image(s)...")

    try:
        with open(log_path, "w") as logfile:
            subprocess.run(
                [
                    str(audiveris_bin),
                    "-batch",
                    "-export",
                    "-output", str(out_dir),
                    *map(str, images)  # Unpacks each image path
                ],
                check=True,
                stdout=logfile,
                stderr=subprocess.STDOUT
            )
    except subprocess.CalledProcessError:
        log.warning("No MusicXML from batch — falling back to per-image Audiveris mode...")

        # === Fallback: Per-image mode ===
        for img in images:
            log.info(f"Running Audiveris on: {img.name}")
            per_image_log = out_dir / f"{img.stem}_audiveris.log"
            try:
                with open(per_image_log, "w") as logfile:
                    subprocess.run(
                        [
                            str(audiveris_bin),
                            "-batch",
                            "-export",
                            "-output", str(out_dir),
                            str(img)
                        ],
                        check=True,
                        stdout=logfile,
                        stderr=subprocess.STDOUT
                    )
            except subprocess.CalledProcessError:
                log.warning(f"Audiveris failed for {img.name}")

# === MuseScore fallback ===
def try_musescore_fallback(input_file: Path, out_dir: Path) -> list[Path]:
    """
        Fallback method to convert a PDF to MusicXML using MuseScore's CLI export.

        Parameters:
            input_file (Path): Path to PDF file.
            out_dir (Path): Directory to store the converted MusicXML.

        Returns:
            list[Path]: List containing the generated .mxl file, or empty list on failure.

        Only works with PDF inputs. Ignored for image files.
        """

    musescore = shutil.which("musescore3") or shutil.which("mscore") or shutil.which("musescore")
    if not musescore or input_file.suffix.lower() != ".pdf":
        return []
    output_xml = out_dir / (input_file.stem + ".mxl")
    log.info("Running MuseScore fallback...")
    try:
        subprocess.run([musescore, str(input_file), "-o", str(output_xml)], check=True)
        if output_xml.exists():
            return [output_xml]
    except subprocess.CalledProcessError:
        log.error("MuseScore fallback failed.")
    return []

# === MusicXML → MIDI ===
def convert_to_midi(mp3_base: str, mxl_files: list[Path], out_dir: Path) -> Path | None:
    """
        Convert one or more MusicXML files into a single MIDI file.

        Parameters:
            mp3_base (str): Base name for output MIDI file.
            mxl_files (list[Path]): List of MusicXML (.mxl/.xml) files.
            out_dir (Path): Directory to save the MIDI file.

        Returns:
            Path | None: Path to the generated MIDI file, or None if failed.

        Additional Features:
        - Removes repeat marks and tempo anomalies.
        - Inserts consistent tempo (160 BPM).
        - Applies quantization to fix note timing artifacts.
        """

    midi_path = out_dir / f"{mp3_base}.mid"
    log.info("Converting MusicXML to MIDI...")

    try:
        if len(mxl_files) == 1:
            score = converter.parse(mxl_files[0])
        else:
            score = stream.Score()
            for f in natsorted(mxl_files):
                part = converter.parse(f)
                score.append(part)

        if transpose_interval != 0:
            log.info(f"Transposing all notes by {transpose_interval} semitone(s)...")
            score = score.transpose(transpose_interval)

        # Remove broken repeat marks
        for el in score.recurse():
            if el.classes and ("Repeat" in el.classes or "RepeatBracket" in el.classes):
                el.activeSite.remove(el)

        # Clean tempo and add uniform tempo
        for t in score.recurse().getElementsByClass(tempo.MetronomeMark):
            t.activeSite.remove(t)
        score.insert(0, tempo.MetronomeMark(number=160))

        if left_hand and right_hand:
            raise ValueError("Cannot enable both left_hand and right_hand simultaneously.")

        # Filter by pitch (left or right hand) before making monophonic
        if left_hand:
            log.info("Filtering for left hand")
            score = filter_score_by_pitch(score, max_pitch=60)

        elif right_hand:
            log.info("Filtering for right hand")
            score = filter_score_by_pitch(score, min_pitch=61)

        # Now apply monophonic strategy if given
        if strategy is not None:
            log.info(f"MIDI conversion for strategy: {strategy}")
            score = make_score_monophonic(score, strategy=strategy)

        score.quantize(inPlace=True)
        score.write("midi", fp=str(midi_path))
        note_data = extract_midi_note_sequence(midi_path)
        json_path = midi_path.with_suffix(".json")
        save_note_sequence_as_json(note_data, json_path)
        log.info(f"Note sequence saved: {json_path}")
        log.info(f"MIDI saved: {midi_path}")
        return midi_path
    except Exception as e:
        log.error(f"MIDI conversion failed: {e}")
        return None

# === MIDI → MP3 ===
def convert_midi_to_mp3(midi_path: Path, mp3_path: Path):
    """
        Convert a MIDI file to MP3 using FluidSynth and FFmpeg.

        Parameters:
            midi_path (Path): Path to the input .mid file.
            mp3_path (Path): Output path for the final MP3.

        Workflow:
        - FluidSynth renders the MIDI to a WAV file using the configured SoundFont.
        - FFmpeg normalizes and encodes the WAV file to MP3.
        - The temporary WAV is deleted after use.

        Plays the MP3 if successfully created.
        """

    if not midi_path or not midi_path.exists():
        log.error("No valid MIDI to convert.")
        return

    wav_path = midi_path.with_suffix(".wav")
    log.info("Converting MIDI → MP3 with normalization...")
    try:
        subprocess.run([
            "fluidsynth", "-ni", str(soundfont_path), str(midi_path),
            "-F", str(wav_path), "-r", "44100", "-g", "1.0"
        ], check=True)
        subprocess.run([
            "ffmpeg", "-y", "-i", str(wav_path),
            "-filter:a", "loudnorm",
            str(mp3_path)
        ], check=True)
        wav_path.unlink(missing_ok=True)
        log.info(f"MP3 created: {mp3_path}")
        play_audio(mp3_path)
    except subprocess.CalledProcessError:
        log.error("Error converting MIDI to MP3.")

# === Pipeline ===
def process_input(input_file: Path, output_dir: Path):
    """
        Full pipeline for processing a single sheet music input file.

        Parameters:
            input_file (Path): Input file (PDF or image).
            output_dir (Path): Root output directory.

        Workflow:
        - Converts input to image(s).
        - Runs Audiveris to generate MusicXML.
        - Falls back to MuseScore if Audiveris fails.
        - Converts MusicXML → MIDI → MP3.
        - Plays final audio output.
        """

    base_name = input_file.stem
    work_dir = output_dir / base_name
    image_dir = work_dir / "images"
    work_dir.mkdir(parents=True, exist_ok=True)

    images = convert_to_images(input_file, image_dir)
    if not images:
        log.error("No images found or converted.")
        return

    run_audiveris(images, work_dir)

    mxl_files = list(work_dir.rglob("*.mxl")) or list(work_dir.rglob("*.xml"))
    if not mxl_files:
        log.info("Trying MuseScore fallback...")
        mxl_files = try_musescore_fallback(input_file, work_dir)

    if not mxl_files:
        log.error("No MusicXML files found.")
        return

    midi_path = convert_to_midi(base_name, mxl_files, work_dir)
    mp3_path = work_dir / f"{base_name}.mp3"
    convert_midi_to_mp3(midi_path, mp3_path)

# === Entry Point ===
if __name__ == "__main__":
    check_dependencies()
    if not input_file.exists():
        log.error(f"Input file not found: {input_file}")
        exit(1)
    process_input(input_file, output_dir)
