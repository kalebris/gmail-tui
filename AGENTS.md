# Agent notes

## UI latency is paramount

Keypresses must feel instantaneous. Treat **least latency on the UI thread** as a hard requirement for mail actions (`j`/`k`, `x`, `d`, `e`, archive, mark, open next row).

- Do **not** run SQLite writes, folder recounts (`list_label_info` / `reload_labels`), MIME re-parses, or Gmail calls on the key that updates the screen.
- Leave-view actions (delete/archive from Inbox) drop the row in memory first; persist labels on the account thread (`label_todo` / `UrgentWork`).
- Paint the frame for the key **before** poking background work that may lock the store.
- Prefer deferring `labels_stale` until after the deferred store write lands.
- When speeding one path (e.g. `x`), keep peer paths (`d`/`e`) in the same budget — do not leave them doing extra local work.

Regression guards live in `tests/test_speed.py`. Extend those tests when you change a hot path; a wall-clock bound alone is not enough — also assert which store/IO APIs the key is allowed to touch.

## Background work

Network, history polls, body cache, and mutation push belong on the account thread. The UI records intent and observes results; it must not wait on Gmail for navigation or leave-view label changes.
