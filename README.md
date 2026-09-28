# Vitals Monitor

A runnable service + clinical test UI around
[A0158/Vitalsigns_Prediction](https://github.com/A0158/Vitalsigns_Prediction),
which predicts vital signs from face video using remote photoplethysmography
(rPPG) with the MTTS-CAN model.

## Reference

Heart rate and respiratory rate come from **MTTS-CAN**, published as:

> Xin Liu, Josh Fromm, Shwetak Patel, Daniel McDuff.
> *Multi-Task Temporal Shift Attention Networks for On-Device Contactless
> Vitals Measurement.* Advances in Neural Information Processing Systems 33
> (NeurIPS 2020).
> [papers.nips.cc](https://papers.nips.cc/paper/2020/file/e1228be46de6a0234ac22ded31417bc7-Paper.pdf)

`vitals/model.py` and `mtts_can.hdf5` are that architecture and its pretrained
weights, vendored from the upstream repository.

**The paper covers heart rate and respiratory rate only.** It does not cover
the SpO&#8322; heuristic or the blood-pressure estimator in this service —
neither of those is backed by any published work, and the citation must not be
read as validating them. The UI no longer states this: the disclosure banner
that carried it was removed on request, so in the interface the distinction now
rests on the amber styling of the estimated tier and its caption,
*ປະເມີນ — ບໍ່ໄດ້ຮັບການກວດສອບ* ("estimated — not validated").

> **Research demo — not a medical device.** The model is unvalidated academic
> work. Do not use these numbers for diagnosis, triage, or any clinical
> decision.

## Run

```bash
docker build -t vitals-monitor:1.0 .
docker run -d --name vitals -p 8080:8080 --restart unless-stopped \
    -v vitals-data:/data vitals-monitor:1.0
```

Open <http://localhost:8080>. The interface is in Lao (ພາສາລາວ), set in
`static/index.html`. The first screen is a login; create an account on the
**ສ້າງບັນຊີ** tab — see [Accounts](#accounts).

The `-v vitals-data:/data` mount is not optional in practice. Accounts and the
session signing key live there, and without it both are inside the container
layer, so every rebuild silently makes all existing logins invalid.

**Capture flow.** One button opens the camera full screen, counts down 3
seconds, records for the selected length, then closes the camera and starts
analysis automatically. The 3-second lead-in is not cosmetic: a camera's
auto-exposure and white balance are still settling in the first seconds after
it starts, and that drift lands directly in the rPPG signal, so those frames
are deliberately excluded. Cancel, Esc, or leaving fullscreen aborts cleanly
and releases the camera.

Camera access needs `localhost` or HTTPS — browsers refuse it on a plain-HTTP
LAN address. File upload works from any origin. Use `localhost`, not the machine's LAN IP —
browsers only grant camera access on a secure origin, and `localhost` counts
while a plain-HTTP LAN address does not. File upload works from any origin.

## Accounts

The capture and scoring endpoints handle patient health data, so they are not
open to anyone who can reach the port. `GET /` and `GET /api/health` are the
only unauthenticated routes; everything else returns `401` without a session.

Registration is open — there is no invite or admin role. For a single-clinic
prototype on a LAN that is the right trade: the alternative is bootstrapping an
administrator account, which is one more secret to distribute and to leak. If
this is ever exposed beyond a trusted network, close registration first.

**What is stored.** Username, display name, a scrypt password hash, and two
timestamps, in a SQLite file at `VITALS_DB` (`/data/vitals.db` in the
container). No patient data is written to it: measurements are computed per
request and returned, never persisted. Nothing is stored that links a clinician
to a clip.

**Password hashing** is `hashlib.scrypt` at the RFC 7914 interactive
parameters (N=2¹⁴, r=8, p=1), with a 16-byte random salt per user and the cost
parameters written into each record — so raising the cost later does not lock
out existing accounts. Verification is `hmac.compare_digest`. A login for an
unknown username still runs a hash, so the response time does not reveal which
usernames exist.

**Sessions** are Flask's signed cookies: `HttpOnly`, `SameSite=Lax`, 7 days.
Nothing but the user id goes in the cookie, and the account row is re-read on
every request, so deleting a user takes effect immediately rather than when
their cookie expires.

**Brute force.** Eight failed attempts on a username within 15 minutes return
`429` until the window passes. The counter is in-process memory and resets on
restart — enough to blunt a script against one account, not a defence against a
distributed campaign. A reverse proxy with real rate limiting belongs in front
of this if it is ever internet-facing.

**Signing out** clears the screen as well as the session: the patient
identifier, the cuff calibration, the risk factors and the results all go, so
the next person at the workstation does not inherit them.

No dependency was added for any of this — `sqlite3`, `hashlib` and `hmac` are
standard library, and `requirements.txt` is unchanged.

### Account settings

| Variable | Default | Purpose |
|---|---|---|
| `VITALS_DB` | `/data/vitals.db` (container), `./vitals.db` (local) | SQLite account store. |
| `SECRET_KEY` | generated once into `.flask-secret` beside the database, mode 0600 | Session cookie signing key. Changing it invalidates every session. |
| `COOKIE_SECURE` | unset | Set to `1` behind TLS. Left off by default because a `Secure` cookie is never sent over the plain HTTP this demo runs on, which looks exactly like a login that succeeds and then does nothing. |
| `SESSION_DAYS` | `7` | Session lifetime. |

There is no password reset and no admin UI. To remove an account:

```bash
docker exec vitals python -c "import sqlite3; \
  sqlite3.connect('/data/vitals.db').execute(\
  \"DELETE FROM users WHERE username='someone'\").connection.commit()"
```

## Stroke risk

A stroke-risk panel sits below the vitals. **The score is computed by a
validated instrument, not by a language model.** A model asked for a risk
percentage returns a fluent, confident, uncalibrated one; that is precisely the
failure this design avoids.

### What is implemented

**CHA&#8322;DS&#8322;-VASc, exactly.** Its point structure is unambiguous and
universally agreed, so what `vitals/stroke.py` returns is the real score.
Annual ischaemic-stroke rates per score come from Michigan Medicine (2021),
NCBI Bookshelf [NBK579414](https://www.ncbi.nlm.nih.gov/books/NBK579414/table/fm.s1.t2/)
table 2. Scores 7 and 8 are *not* monotonic with 6 in that cohort — those
strata are small — and are reported as published rather than smoothed, with the
UI flagging the uncertainty.

**Every patient gets an overall assessment.** Atrial fibrillation is not a
gate; it only decides whether a validated *percentage* can be attached:

* **Always** — an overall band (`low` / `moderate` / `high`) from risk-factor
  burden: how many of eight established stroke risk factors are present
  (hypertension, diabetes, smoking, AF, prior stroke/TIA, vascular disease,
  heart failure, age ≥65). This is a count of recognised risk factors, reported
  as such, and is the headline result.
* **With AF only** — the CHA&#8322;DS&#8322;-VASc annual percentage, shown
  beneath the overall band. CHA&#8322;DS&#8322;-VASc was derived and validated
  in atrial fibrillation, so `annual_risk_percent` stays `null` outside it
  rather than attaching a probability to a population the instrument was never
  fitted to.

The AI narrative is an **overall** one: it receives the measured vitals from
the most recent capture alongside the entered risk factors, and covers both.

### What is deliberately missing

The **Framingham Stroke Risk Profile** (Wolf/D'Agostino, *Stroke*
1991;22:312–318) is the right instrument for a 10-year risk in a general
population, and `framingham_available()` marks where it belongs. It is **not
implemented**: the published point tables are paywalled, three retrieval
attempts failed, and the secondary literature cites them without reproducing
them. Inventing them would produce a number carrying the authority of a
validated instrument without being one. Supply the tables and it drops in.

### The model's role

`vitals/explain.py` sends the *already computed* result to FreeLLMAPI and asks
for a Lao explanation of what drives the risk and what is modifiable. It is
forbidden from producing numbers — and that instruction is not trusted on its
own. `_verify` re-reads the generated text and rejects it outright if any
percentage appears that the scorer did not produce, so a model that ignores the
prompt fails closed to the plain numeric readout.

**Provider chain.** Explanations try FreeLLMAPI first, then fall back to
**SEA-LION** (AI Singapore) called directly. SEA-LION is more than a spare
here: the family is trained on Southeast Asian languages including Lao, and on
this task `Gemma-SEA-LION-v4-27B-IT` held 84–89% Lao script where a general
model drifts to English. It is reached directly rather than through the
gateway, so a gateway outage does not take the backup down with it.

Two other SEA-LION models were tested and rejected:
`Qwen-SEA-LION-v4.5-27B-IT` is a reasoning model that spent its whole budget on
hidden `reasoning_content` and returned empty text with
`finish_reason: length` (`_call` now treats that as a failure and moves on),
and `Llama-SEA-LION-v3-70B-IT` produced fluent Lao but garbled the clinical
terms.

The numeric guard runs **per provider**, so a backup is not trusted more than
the primary just because it answered last. It allows the measured vitals as
well as the score: SpO&#8322; is itself a percentage, and an early version
rejected a model correctly reporting "96.2%" as an invention. The guard exists
to catch fabricated *risk* figures, not to stop the model repeating a
measurement it was handed.

The explanation is never a precondition. If every provider is down,
unconfigured, or misbehaves, the score and percentage still stand and the UI
says why the prose is missing.

Credentials live in `~/vitalsigns/.llm-env` (0600), read by `autostart.sh` on
every start. Nothing is baked into the image.

```bash
curl -X POST http://localhost:8080/api/stroke-risk \
  -d age=78 -d female=1 -d atrial_fibrillation=1 -d hypertension=1 -d diabetes=1 -d explain=1
```

## LLM gateway (FreeLLMAPI)

[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) runs as a separate
container, aggregating free provider tiers behind one OpenAI-compatible
endpoint. It is bound to loopback and joined to the `clinic` Docker network so
the vitals service can reach it as `http://freellmapi:3001`.

```bash
docker run -d --name freellmapi --env-file ~/freellmapi/.env \
  -e NODE_ENV=production -e PORT=3001 -p 127.0.0.1:3001:3001 \
  -v freellmapi-data:/app/server/data \
  --add-host host.docker.internal:host-gateway --restart unless-stopped \
  ghcr.io/tashfeenahmed/freellmapi:latest
docker network connect clinic freellmapi
```

**It needs setup before explanations work:** open <http://localhost:3001>,
create the first account, add at least one provider key on the Keys page, then
copy the unified API key from that page's header into the vitals container as
`LLM_API_KEY` (set it in `autostart.sh` or pass it through). Until then
`/api/stroke-risk` returns the score with `explanation_error` set.

Note FreeLLMAPI's own disclaimer: it is built for personal experimentation, not
production, and free provider tiers are not a supported inference substrate.

## Autostart

The service comes back on its own after a reboot. Three layers, from the inside out:

| Layer | Mechanism | State |
|---|---|---|
| Container | `--restart unless-stopped` | set |
| Docker daemon | `docker.service` enabled under WSL systemd (`systemd=true` in `/etc/wsl.conf`) | already enabled |
| WSL distro | Windows scheduled task **“Vitals Monitor autostart (WSL)”**, at logon, +20 s delay | registered |

WSL does not boot with Windows on its own — it starts only when something asks
for it. The scheduled task is what asks. It runs
`wsl.exe -d Ubuntu-26.04 -u root /home/kobi/vitalsigns/autostart.sh`, which
boots the distro; systemd then starts Docker and the restart policy brings the
container back.

`autostart.sh` is a safety net rather than the primary mechanism. It waits up
to 60 s for dockerd, then starts the container, or recreates it from the image
if it has been removed — covering the cases the restart policy does not.
It is safe to run by hand at any time.

**Verified end to end:** the container was deleted entirely, the task run from
Windows, and the service came back healthy on `localhost:8080` (task exit
code 0).

### Managing it

```powershell
# check
Get-ScheduledTask -TaskName 'Vitals Monitor autostart (WSL)'

# run it now
schtasks /Run /TN "Vitals Monitor autostart (WSL)"

# turn autostart off / back on
Disable-ScheduledTask -TaskName 'Vitals Monitor autostart (WSL)'
Enable-ScheduledTask  -TaskName 'Vitals Monitor autostart (WSL)'

# remove entirely
Unregister-ScheduledTask -TaskName 'Vitals Monitor autostart (WSL)' -Confirm:$false

# reinstall (re-running is safe, it replaces the existing task)
powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu-26.04\home\kobi\vitalsigns\vitals-autostart-setup.ps1
```

Two things to expect:

* **A console window flashes briefly at logon.** `wsl.exe` is a console
  program, and the usual fix (`conhost --headless`) is Windows 11 only — this
  machine is Windows 10 19045. The task is marked hidden, which is as far as
  Windows 10 goes without switching to a service-context principal that would
  make WSL launch less reliable.
* **Logging in now boots the whole WSL VM**, Docker and the TensorFlow
  container included — on the order of a gigabyte of RAM that previously was
  only used once you opened a terminal. Disable the task if that is not a
  trade you want.

If the image is ever rebuilt under a new tag, update `IMAGE` at the top of
`autostart.sh` (or set `VITALS_IMAGE`), otherwise the recreate path will
resurrect the old version.

## What actually works

| Vital | Status |
|---|---|
| Heart rate | **Working.** MTTS-CAN pulse branch, FFT peak in the 0.75–2.5 Hz band. |
| Respiratory rate | **Working.** MTTS-CAN respiration branch, 0.08–0.5 Hz band. |
| SpO&#8322; | **Experimental.** The upstream `100 - 5R` red/blue variance heuristic. Uncalibrated — no reference oximeter data exists in the repo. Treat as a trend, not a measurement. |
| Blood pressure | **Experimental, added here.** No BP model exists upstream, so this is estimated from pulse-waveform morphology. Read the caveat below before trusting any number it shows. |

## Blood pressure — read this

Upstream ships no BP model: its README claims blood pressure, but the only
reference is to a PPG2ABP network in `evaluate.py` whose weights were never
included. `vitals/bloodpressure.py` therefore estimates BP from the shape of
the pulse waveform MTTS-CAN already recovers.

**The physiology is real; the coefficients are not trained.** Arterial
stiffness rises with blood pressure, and a stiffer arterial tree gives a faster
systolic upstroke and a narrower pulse, so systolic upstroke time (SUT), pulse
width at half amplitude, and heart rate genuinely carry BP information. Their
*signs* are not in dispute. But fitting the *magnitudes* honestly needs paired
PPG and arterial-line data (MIMIC-III or equivalent), which this repository
does not have. The constants are physiologically scaled and anchored on
population norms (120/80 at 70 bpm), not learned.

What follows from that:

* **Uncalibrated** output is a population-anchored *trend indicator*. Its
  absolute value can be off by a wide margin on any individual.
* **Calibrated** output — supply one reference cuff reading in the UI —
  re-anchors the intercept to that person. Only *change from the reference*
  carries meaning, and it is still unvalidated. Note the calibrated reading
  lands *near*, not exactly on, the reference: the clip's own morphology
  deviation is deliberately retained, since discarding it would discard the
  measurement.

Cuffless BP from PPG alone is an open research problem. Nothing here closes it.

### What the estimator was tested for

Behavioral tests against synthesized PPG waveforms with known morphology:

| Property | Result |
|---|---|
| SUT extraction | Within 17 ms of truth across 60–90 bpm |
| Monotonic in heart rate | Yes, 55→95 bpm |
| Monotonic in arterial stiffness | Yes, SUT fraction 0.30→0.14 |
| Calibration re-anchors to reference | Yes, within ~3 mmHg |
| Flat signal | Refused |
| Broadband noise | Refused (beat-interval scatter 26% > 22% threshold) |

Five defects were found and fixed:

1. **Quantization.** At 30 fps a systolic upstroke spans only 4–6 samples, and
   the rounding made BP *non-monotonic* in heart rate (85 bpm → 130, 95 bpm →
   129). The signal is now resampled 8x before morphology is measured.
2. **Noise accepted as signal.** Broadband noise produced confident-looking
   beats and a BP number. Beat-interval scatter is now a rejection gate.
3. **Unbounded extrapolation.** A waveform with non-physiological morphology
   dragged a *calibrated* reading 34 mmHg away from its own reference. The
   morphology ratios now saturate at physiological bounds and their combined
   contribution is capped at ±25 mmHg.

4. **Refused on real recordings.** The noise gate was tuned against clean
   synthetic waveforms and pure noise with nothing in between — but real rPPG
   sits exactly in that middle, so BP was refused on essentially every genuine
   webcam clip. The cause was not the threshold: at low SNR the peak detector
   *invented* beats (20 found where 18 existed), inflating the irregularity
   measure. Beat detection is now steered by the heart rate the FFT already
   recovered, which comes from the whole clip's spectrum and is far more
   robust than any individual peak. Intervals outside 0.6–1.6x the expected
   period are excluded before regularity is measured, since a missed or
   spurious beat says nothing about the underlying rhythm. BP now resolves
   down to an SNR of 0.33.
5. **Blank tile instead of a degraded one.** When morphology could not be
   resolved the tile showed nothing, even though the heart rate itself was
   usually sound. It now falls back to a rate-only estimate, reported with
   `quality: "rate_only"`, a low-quality badge, and its confidence scaled down.

Because the fallback made it possible to report a number for a clip with no
pulse at all, BP is now additionally gated on the pipeline's spectral
confidence (`MIN_HR_CONFIDENCE = 0.45`). That threshold was measured, not
guessed: across many clips, broadband noise with no pulse tops out near 0.36
while a real pulse stays above 0.50 down to SNR 0.33. Verified against 12
pure-noise clips, a flat signal, constant DC, and an impossible heart rate —
all refused.

None of this makes the numbers clinically valid. It makes them behave
sensibly and fail loudly.

### Known behaviour: calibrated readings do not land on the reference

Calibration re-anchors the heart-rate term but deliberately keeps the clip's
own morphology deviation, so entering a cuff reading of 120/80 may show, say,
104/71 on that same clip. This is by design — discarding the deviation would
discard the measurement — but it means single-point calibration cannot make the
displayed number match the cuff.

Fixing that properly needs a calibration *clip*: capture the waveform features
at the moment the cuff reading is taken, store them, and report
`reference + (model(now) - model(then))` afterwards. That is the correct
semantics for cuffless BP tracking and is not yet implemented; the API is
stateless and takes only the cuff numbers.

### Accuracy check

Validated against synthesized clips with a known modulation frequency:

| Clip | True HR | Measured HR |
|---|---|---|
| 1 | 72.0 bpm | 73.2 bpm |
| 2 | 96.0 bpm | 94.6 bpm |

Both are within one FFT bin (3.05 bpm at 20 s). Respiratory rate could **not**
be validated this way: the synthetic stimulus was a vertical image translation,
whose frame-difference energy peaks twice per cycle, so it reads out at exactly
2x the true rate. RR remains unverified against real breathing.

## API

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/health` | open | Liveness, whether ffmpeg is present, and the signed-in user (`null` if none). |
| `POST /api/auth/register` | open | `{username, password, full_name}`. Creates the account and signs in. `409` if the username is taken. |
| `POST /api/auth/login` | open | `{username, password}`. `401` on bad credentials, `429` when throttled. |
| `POST /api/auth/logout` | open | Clears the session. |
| `GET /api/auth/me` | session | The signed-in clinician, or `401`. |
| `POST /api/warmup` | session | Build the TensorFlow graph ahead of the first upload. |
| `POST /api/analyze` | session | `multipart/form-data` with a `video` field. Optional `cal_systolic`, `cal_diastolic`, `cal_hr` re-anchor the BP estimate. Returns rates, confidences, BP, and normalized waveforms. |
| `POST /api/stroke-risk` | session | CHA₂DS₂-VASc scoring, optionally with a generated explanation. |

Both auth routes accept JSON or form encoding.

```bash
# sign in once and keep the session cookie
curl -sc jar -X POST http://localhost:8080/api/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"dr.somchai","password":"..."}'

# uncalibrated
curl -b jar -X POST -F "video=@clip.mp4" http://localhost:8080/api/analyze

# anchored to a reference cuff reading of 128/82 taken at 74 bpm
curl -b jar -X POST -F "video=@clip.mp4" \
     -F "cal_systolic=128" -F "cal_diastolic=82" -F "cal_hr=74" \
     http://localhost:8080/api/analyze
```

Errors: `400` bad request or file type, `401` no or expired session,
`409` username taken, `413` over the 200 MB limit, `422` unusable clip
(no face, too short), `429` login throttled, `500` inference failure.

## What was changed from upstream

The original repository does not run. Fixes required to get it serving:

**`app.py`** — imported a nonexistent `opencv` package; registered two view
functions both named `predict_vitals`, which Flask rejects as a duplicate
endpoint; unpickled a `classifier.pkl` absent from the repo; imported modules
from paths that do not resolve; and passed a `FileStorage` where a file path was
expected. Rewritten.

**`requirements.txt`** — pinned 2018-era builds (`tensorflow-gpu==1.5.0`,
`opencv-python==3.4.0`, `numpy==1.17.0`, `scikit-learn==0.19.1`) that build on
no current Python. Replaced. TensorFlow is held at **2.15** deliberately:
`vitals/model.py` calls `K.int_shape`, removed in the Keras 3 default of
TF 2.16+.

**`inference_preprocess.py`** — returned `(dXsub, fps)` while `predict_video.py`
unpacked it as a single value; called `plt.show()` mid-pipeline, which blocks a
server thread; ran face detection on every frame with no fallback, so a single
missed detection reused a stale `roi` from the previous loop iteration. The
detrend built an N x N dense identity and inverted it — O(N^2) memory, O(N^3)
time — which a 30 s clip cannot afford. Now sparse, with a cached face box.

**`predict_video.py`** — took an argparse namespace, printed its results, and
returned nothing. Its frequency axis was hardcoded to `1./30` regardless of the
clip's real frame rate, so any video not shot at 30 fps produced a
proportionally wrong rate. Now takes the measured fps.

**`oxygensaturation.py`** — ran the webcam capture at import time, required
`face_recognition` (dlib, not in requirements), and summed pixels in a pure
Python double loop over every frame. Vectorized and moved off the import path.

Original files are preserved unmodified in `upstream/` for reference.

## Browser testing

The UI is exercised end to end in headless Chromium, with Chrome's fake capture
device fed a synthesized face video
(`--use-file-for-fake-video-capture`), so the camera path is covered without a
physical webcam:

* fullscreen overlay opens, video fills the viewport (`object-fit: cover`),
* 3-second lead-in, then the recording countdown,
* auto-close, camera track released, analysis fires and populates every tile,
* cancel during lead-in and during recording both release the camera and leave
  the previous results untouched,
* Noto Sans Lao loads and is the computed font, with no console errors.

## Design

The interface was reworked using Anthropic's `frontend-design` skill. The brief
was pinned first: a contactless vitals monitor for a Lao hospital ward, read by
nurses, whose single job is to capture a clip and return vitals with the
boundary between *measured* and *estimated* impossible to miss. Green and white
and Lao/Noto Sans Lao were fixed by the user's own brief and were kept exactly.

**Palette** — greens pushed deeper, with the white carrying a faint green cast
so the page reads as one family rather than a neutral sheet with an accent
bolted on: `--paper #FBFCFA`, `--ink #0B1F15` (a green-black, not a neutral
grey), `--vital #0A6B3D`, `--trace #14A862`, `--caution #8A5A00`. Amber is
rationed: it appears only on things that are not validated, nowhere else.

**Type** — two roles. Noto Sans Lao carries all Lao text; **IBM Plex Mono**
carries every numeral, unit, timestamp and identifier. The justification is the
subject: this is an instrument, its readings should read as instrument output,
and tabular figures stop values jittering as they update.

**Signature — the signal strip.** rPPG is light reflected off skin turned into
a line, which is the truest image of what this product does, so that line is
the page's spine: full width, dark, with the heart rate set *inside* it rather
than in a tile beside it. The trace reports its own reliability — a confident
pulse draws as a solid glowing line, a weak one as a thin dashed one — so the
structure carries information instead of decorating it. One orchestrated
motion: the trace writes itself in over 950 ms like a chart recorder, and is
skipped under `prefers-reduced-motion`.

**Structure encodes the safety boundary.** The most important fact about this
product is which numbers are measured and which are guessed. That used to be a
small badge. It is now the page's structure: two labelled bands, *Measured*
(HR, RR — backed by the published model) and *Estimated* (SpO&#8322;, BP — on a
tinted ground, quieter type), so the eye lands on the trustworthy numbers
first.

Two bugs were found in the build's own critique pass and fixed: the status line
never hid itself (the CSS had been renamed to `.say`/`.prog` while the JS still
wrote `.msg`/`.bar`), and the pulse trace stopped short of the right edge
because the write-on animation used a hardcoded `stroke-dasharray` of 4000
against a path measuring 4749 — it now measures the path.

## Language and typography

The interface is Lao. Text is set in **Noto Sans Lao** (SIL Open Font License
1.1), bundled locally as three woff2 subsets rather than linked from Google
Fonts, so the app renders correctly on a machine with no internet access.

Two details worth keeping if you edit the UI:

* **Line height is 1.75**, well above a Latin-only default. Lao stacks vowel
  signs above and tone marks below the base consonant, and at a tighter leading
  those marks collide with the line above.
* Numerals stay Western Arabic (`font-variant-numeric: tabular-nums`), which is
  standard in Lao clinical documents, and units that are read as symbols —
  mmHg, fps, SpO&#8322;, MAP — are left untranslated.

API error strings are returned in English by the server and mapped to Lao in
the browser (the `ERRORS` table in `index.html`), so the backend stays
language-neutral for other clients.

## Layout

```
app.py              Flask service and API
vitals/model.py     MTTS-CAN architecture (vendored unchanged from upstream)
vitals/preprocess.py  Face crop, frame normalization, sparse detrend
vitals/pipeline.py    Inference, HR/RR/SpO2 extraction
vitals/bloodpressure.py  BP estimation from pulse morphology (added here)
autostart.sh        Ensures the container is running (used by the logon task)
vitals-autostart-setup.ps1  Registers the Windows logon task
static/index.html   Monitor UI (Lao)
static/fonts.css    @font-face declarations
static/fonts/       Noto Sans Lao woff2 subsets, bundled for offline use
mtts_can.hdf5       Pretrained weights (11 MB, from upstream)
upstream/           Original files, for diffing
```
