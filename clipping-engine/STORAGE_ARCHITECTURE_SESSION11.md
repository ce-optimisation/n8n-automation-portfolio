# Raw-recording storage architecture (Session 11)

## What failed on 17 Nov 2026
Rumble "America First Ep. 1780": one recorder wrote a single 11.37 GB `seg_001.ts`. The only guard stopped recording at ~0.5 GB free,
then finalising tried to write a second full-size `recording.mp4` (0.44 GB written) and ffmpeg failed with "No space left on device".
Both files stayed in `data\work\live\src001_v7iial2\`, indefinitely.

## What changed (implemented and tested in CI/Linux; NOT yet run against a real stream on Windows)
| Stage | Before | Now |
|---|---|---|
| Capture admission | one free-space check (0.5 h at lowest quality) | `livestore.admit`: every active capture + the new one must have room for its next segment + join headroom, and the total live buffer must stay under `LIVE_BUFFER_GB` (20); otherwise refused and retried at the next check |
| Segment size | unbounded (11.4 GB) | recorder rotated at `LIVE_SEGMENT_MAX_GB` (1.0) or `LIVE_SEGMENT_MAX_MINUTES` (30); existing restart-overlap trimming joins the parts |
| Low-disk stop | at ~0.5 GB free (too late to join) | while there is still room to join (reserve + 1.5 GB per capture) |
| Join | concat-remux into a second full MP4 | append each (overlap-trimmed, timestamp-continuous) segment to one MPEG-TS and delete that segment only after the append succeeded and was recorded; peak extra = one segment; no MP4 remux |
| Out of space while joining | `failed`, files left behind | append rolled back, state `awaiting_space`, every remaining segment kept, resumes automatically when space returns |
| Crash while joining | restart from scratch | manifest-based resume (no duplicate, no loss) |
| Failed captures | invisible, consumed space forever | listed on the Storage page with exact sizes; removed only by a verified discard (`DISCARD <id>`), or `Recover-FailedRecording.cmd` |
| Disk guard for transcription/render | none | `storage.require_space` (Session 10) |
Recordings are now `.ts` (MPEG-TS) files in the engine; transcription, rendering and probing use ffmpeg directly. The browser cannot play `.ts`
in the "original at this excerpt" preview (download/VLC works) — a known trade-off.

## Free cloud buffer — decision
* **Google Drive** (15.9 GB free, shared with the permanent masters): kept for finished-video masters and metadata only. 11 GB raw files would starve the masters, and
  the buffer would have to hold several of them. Not used for raw footage.
* **MEGA free (20 GB)**: NOT integrated. I could not verify from official sources: the free transfer quota is reported by third parties as tracked per IP address over
  rolling windows with figures that disagree, MEGA's inactivity/deletion rule for free accounts applies, and I found nothing stating that scripted MEGAcmd use is permitted.
  Pulling 10 GB back for processing could stall on the quota. Verify at mega.io/terms and the MEGAcmd user guide before anyone builds on it.
* Chosen design: **bounded local buffer + admission control** (above), process existing footage first, raw deleted by the existing eight-gate policy. Cloud-backed
  temporary raw segments (steps 3–4 and 7 of the brief) are **not implemented**.

## Not implemented in Session 11 (be honest)
Retrieving only the needed footage from cloud; temporary-cloud usage on the dashboard (there is none); concurrent-capture quotas across providers beyond the local buffer;
"transcribe while recording". The eight-gate raw policy is unchanged (it does not know about cloud-backed segments because none exist).
