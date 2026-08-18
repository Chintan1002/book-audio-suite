# TEST PLAN — first automated coverage

The project has **zero** automated tests today; every stage has been verified by
hand. This plan lands the cheap, high-value coverage first and locks the two
correctness bugs (D-1, D-2) behind regression fixtures so they cannot return.

Companion documents: `PRD_QUALITY.md` (what to fix), `QUALITY_TASK.md` (how).

---

## 1. Principles

- **Offline and fast.** No network, no Edge-TTS calls, no whisper model
  download. The suite must run in seconds so it's actually run.
- **Pure functions first.** Most of the real logic is deterministic and
  dependency-free — that's where the value/effort ratio is best.
- **Fixtures over mocks.** Generate small PDFs with PyMuPDF and build
  `TranscriptResult` objects by hand rather than mocking whisper.
- **Every bug fixed gets a test that fails without the fix.** That is the whole
  point of doing this now rather than later.

## 2. Framework

`pytest`, plus `pytest-asyncio` only if async chunk-scheduling tests are added
later.

```
pip install pytest
pytest -q            # from the repo root
```

Layout:

```
tests/
  conftest.py            shared fixtures (tmp PDFs, sample TranscriptResult)
  test_cleaner.py        Stage 2 transforms
  test_chunker.py        tts.chunk_text packing rules
  test_chapters.py       detection + grouping, incl. the D-1 regression
  test_formatter.py      STT paragraph rule
  test_subtitles.py      .srt/.vtt output (once F-2 lands)
  test_app_jobs.py       job persistence + retention (filesystem only)
fixtures/
  d1_header_pageno.pdf   8 pages, header page numbers, 2 real headings, no TOC
  toc_book.pdf           embedded TOC, known chapters
```

## 3. Coverage by module

### 3.1 `cleaner.py` — highest value, zero dependencies

| Test | Asserts |
|---|---|
| `test_dehyphenate` | `"clean-\ning"` → `"cleaning"`; a real hyphenated compound at line end survives |
| `test_strip_page_numbers` | a line that is only a number is removed; `"Chapter 3"` is not |
| `test_strip_running_headers` | a short line repeated ≥3× is dropped; a repeated *sentence* is kept |
| `test_reflow_paragraphs` | single newlines → spaces; blank-line breaks preserved (guards the NUL-sentinel bug that once made this module unimportable) |
| `test_normalize_unicode` | ligatures and smart quotes flattened |
| **`test_normalize_dashes`** ⚑ | **D-2 regression:** `"precisely—having"` → comma pause, *not* `"precisely-having"`; `"1914–1918"` → `"1914 to 1918"`; a plain ASCII hyphen is untouched |
| `test_pipeline_order` | running `DEFAULT_PIPELINE` end to end on a messy sample gives clean prose |

### 3.2 `tts.py` — the chunker (pure half only; no synthesis)

| Test | Asserts |
|---|---|
| `test_chunks_respect_max` | no chunk exceeds `max_chars`, except a single over-long word |
| `test_indices_contiguous` | indices are `0..n-1` in reading order — the ordering invariant everything else depends on |
| `test_no_text_lost` | concatenating chunk text reproduces the input's words |
| `test_hard_split_long_sentence` | a sentence longer than the limit is split on whitespace, not dropped |
| `test_empty_input` | empty/whitespace input yields zero chunks, no exception |

### 3.3 `chapters.py` — where D-1 lives

| Test | Asserts |
|---|---|
| **`test_header_page_numbers_do_not_split`** ⚑ | **D-1 regression:** `d1_header_pageno.pdf` yields **2** chapters (not 8), and no title is a bare number |
| `test_toc_preferred` | with an embedded TOC, detection uses it and ignores heading heuristics |
| `test_toc_titles_exact` | `toc_book.pdf` returns its exact chapter titles and start pages |
| `test_confidence_gate` | a pathological book (a "heading" on every page) falls back to fixed-size Parts |
| `test_group_pages_covers_all` | every non-empty page lands in exactly one segment; front matter handled |
| `test_no_chapters_is_one_segment` | zero detections → a single whole-book segment, not a crash |

### 3.4 `stt/formatter.py`

| Test | Asserts |
|---|---|
| `test_paragraph_break_on_pause` | a gap ≥ 2.0s between segments starts a new paragraph; a smaller gap does not |
| `test_tidy_spacing` | no space before punctuation; runs of whitespace collapsed |
| `test_empty_result` | zero segments → empty string, no exception |

### 3.5 `stt/subtitles.py` (once F-2 lands)

| Test | Asserts |
|---|---|
| `test_srt_format` | correct index, `HH:MM:SS,mmm --> HH:MM:SS,mmm` timing, blank-line separation |
| `test_vtt_header` | file starts with `WEBVTT`, timings use `.` not `,` |
| `test_empty_transcript` | a valid, empty subtitle file — not a crash |

### 3.6 `app.py` — job lifecycle (filesystem only, no server)

| Test | Asserts |
|---|---|
| `test_job_roundtrip` | a `Job` written to `job.json` and rehydrated keeps id, kind, status and output paths |
| `test_rehydrate_skips_corrupt` | a truncated `job.json` is skipped, startup still succeeds |
| `test_retention_deletes_old` | a directory older than the TTL is removed |
| `test_retention_keeps_running` | a non-terminal job is never deleted, regardless of age |

## 4. Explicitly not covered (and why)

- **Edge-TTS synthesis** — network-dependent and rate-limited. The engine seam
  (`async (text, out_path) -> None`) is trivially fake-able, so if the
  concurrency harness is ever tested, inject a stub engine rather than calling
  the real service.
- **faster-whisper accuracy** — a model download and minutes of CPU. Transcript
  *quality* stays a manual check; only our formatting/plumbing around it is
  tested.
- **FFmpeg stitching** — needs the binary and real audio. Covered by the manual
  duration-sum check already in use (stitched duration must equal the sum of the
  parts).
- **Browser UI** — no E2E harness for now; the two flows are verified by hand
  before release.

## 5. Definition of done

- `pytest -q` runs offline from a clean checkout in under ~10 seconds.
- The two ⚑ regression tests **fail on the current code** and pass after the
  fixes — verify that ordering explicitly; a test that never failed proves
  nothing.
- Every subsequent bug fix in this pass arrives with a test.
