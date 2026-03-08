# Product Requirements Document: Analog-to-OTF Pipeline

## 1. Objective
Build an automated, local command-line pipeline that converts a scanned physical template of handwriting into a fully functional, typable OpenType font (`.otf`). The system must accurately capture the unique line variations and shading characteristics of various physical inks, converting high-resolution analog scans into clean digital vectors with precise baseline alignment.

## 2. User & Use Case
A technical user digitizing their own handwriting to use in daily digital environments. The generated font must preserve the natural flow and ink characteristics of the user's handwriting by supporting standard ASCII characters alongside a specific set of OpenType features (ligatures and contextual alternates).

## 3. Technical Architecture & Constraints
The agent must implement this as a modular pipeline. An all-Python architecture is required for simplicity and seamless data handoffs between the image processing and font compilation stages.
* **Core Application:** Python CLI application.
* **Image Processing:** Python with OpenCV (deskewing, binarization, contour extraction, bounding box offset calculation).
* **Vectorization:** Potrace (called via Python bindings or subprocess to convert rasterized character contours into smooth SVGs).
* **Font Assembly:** Python with FontForge bindings (mapping SVGs to Unicode slots, defining OpenType lookup tables, setting bearings, compiling).
* **Input:** A 600 DPI grayscale or color `.png` or `.pdf` scan.
* **Output:** A compiled `.otf` file.

## 4. Core Modules & Agent Tasks

### Module A: Template Generator
* **Requirement:** A Python script that generates a printable A4/Letter PDF grid.
* **Features:**
  * Registration marks (e.g., ArUco markers or solid squares) in the four corners for automated alignment.
  * Clearly defined bounding boxes for standard ASCII characters.
  * Additional labeled bounding boxes for specific ligatures and alternates: `fi`, `fl`, `ff`, `tt`, `th`, `oo`, `ee`, `ll`, `ss`, `mm`, `nn`, `or`, `os`, `ve`, `we`, `br`.
  * **Baseline Marker:** A faint, detectable horizontal line across the bottom 25% of each bounding box to guide the physical writing and the OpenCV extraction script.

### Module B: Computer Vision & Extraction Pipeline
* **Requirement:** A Python script to process the scanned physical template.
* **Features:**
  * Detect registration marks and apply a perspective transform to deskew and flatten the image.
  * Apply adaptive thresholding/binarization to cleanly separate the ink from the paper background, ensuring highly saturated or shading inks do not create artifacts.
  * Slice the grid into individual image files corresponding to each character/ligature.
  * **Baseline Calculation:** Calculate and store the vertical distance (Y-offset in pixels) between the lowest point of the ink contour and the printed baseline marker.
  * Tightly crop the whitespace around the ink contours in each sliced image and output the image along with its Y-offset metadata.

### Module C: Vectorization & Font Compilation
* **Requirement:** A Python script using FontForge bindings to convert the extracted images into a font file.
* **Features:**
  * Run Potrace on each cropped image to generate an SVG path, optimizing parameters to smooth jagged pixel edges.
  * Initialize a new FontForge object.
  * Map standard character SVGs to their respective Unicode hex values, and ligature/alternate SVGs to unencoded glyph slots.
  * **Vertical Alignment:** Apply the Y-offset metadata to set the vertical bearing (Y-min) for each glyph, ensuring descenders (p, q, y, g, j) drop correctly below the FontForge coordinate baseline.
  * Programmatically write the OpenType Feature (OTF) lookup tables (`liga`, `calt`, `dlig`) to substitute standard characters with the custom bigrams and alternates.
  * Calculate proportional horizontal side bearings based on the bounding box width of each SVG.
  * Export as `Handwriting_MVP.otf`.

### Module D: Baseline Testing & Auto-Nudging (Optional / Phase 2)
* **Requirement:** An automated cleanup pass before final compilation to correct minor human drift.
* **Features:**
  * Calculate a "median baseline height" using the Y-offsets of standard flat-bottomed characters (e.g., a, c, e, m, n, o, x).
  * Establish a tolerance threshold (e.g., +/- 5 pixels).
  * Automatically snap the Y-offset of anomalous standard characters to the median baseline if they fall within the tolerance threshold.
  * Generate a visual HTML or PDF proof sheet showing the generated font aligned on a ruled grid to verify baseline consistency before final OTF export.

### Module E: Autotune (Optional build-time optimization pass)
* **Requirement:** An optional build mode that iteratively improves glyph extraction geometry and spacing without changing the existing proof-sheet design.
* **Features:**
  * Run as an explicit build option (for example, `build --autotune`) so the pipeline remains reproducible both with and without autotuning.
  * Analyze extracted glyph bitmaps, vector outlines, and rendered specimen text to detect visual incongruence such as:
    * inconsistent left/right side bearings,
    * over-tight or over-wide glyph crops,
    * accidental inclusion of box borders or nearby printed labels,
    * mismatched x-height or cap-height alignment,
    * descenders or ascenders that visually collide despite nominal kerning values.
  * Generate additional internal tuning specimens that exercise common failure modes more aggressively than the normal proof sheets do (e.g., repeated doubles, mixed descender/ascender pairs, punctuation combinations, and spacing stress tests). These specimens are for autotune analysis only and must not change the user-facing proof workflow.
  * Iteratively adjust per-glyph extraction boxes and/or derived placement metadata, re-render tuning specimens, and stop when the score converges or a configured iteration limit is reached.
  * Propose or emit kerning configuration overrides alongside glyph-box adjustments so the final output is both visually improved and reproducible from configuration, not from one-off manual edits.
  * Emit a machine-readable and human-readable autotune log describing every change, including the glyph or pair affected, the old and new values, the reason for the adjustment, and the iteration in which it occurred.
  * Support a dry-run mode that reports suggested changes without applying them.
* **Constraints:**
  * Autotune must preserve deterministic builds: given the same inputs and config, it should produce the same tuned output and the same emitted settings.
  * Autotune output should be representable as config values that can be checked into source control (for example, glyph box overrides, nudges, hshifts, and kerning pairs).
  * The normal proof command and proof layout should remain unchanged; autotune may create separate analysis artifacts as needed.

## 5. Success Criteria
* The agent successfully generates a print-ready PDF template with baseline guides.
* The OpenCV pipeline correctly calculates and passes Y-offset metadata to FontForge within the Python environment.
* The processing pipeline runs end-to-end via a single Python CLI command without manual intervention.
* The build can be run with or without autotune, and autotune emits a clear log of the adjustments it applied.
* The output `.otf` file installs cleanly on a local operating system.
* Typing double letters (e.g., "oo") automatically triggers the OpenType substitution rule.
* Letters with descenders visually drop below the baseline when typed.
