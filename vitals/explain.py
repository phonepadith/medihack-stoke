"""Lao-language explanation of a stroke-risk result, via FreeLLMAPI.

The division of labour is deliberate and load-bearing: `vitals/stroke.py`
computes the number, this module only puts it into words. The model is told the
already-computed values and is forbidden from producing any of its own, because
a language model asked for a risk percentage returns a fluent, confident,
uncalibrated one. Nothing here may change a number.

That instruction is not trusted on its own. `_verify` re-reads the generated
text and rejects it if any percentage appears that the scorer did not produce,
so a model that ignores the prompt fails closed to the plain numeric readout
rather than putting an invented figure in front of a clinician.
"""
import json
import os
import re
import urllib.error
import urllib.request

GATEWAY = os.environ.get("LLM_GATEWAY", "http://freellmapi:3001/v1/chat/completions")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "auto")
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "45"))

# SEA-LION (AI Singapore) as the backup provider. It is worth more than a
# fallback here: the SEA-LION family is trained on Southeast Asian languages
# including Lao, and on this task it held 89% Lao script where a general model
# drifts to English. Reached directly rather than through the gateway so a
# gateway outage does not take it down too.
SEALION_URL = os.environ.get("SEALION_URL", "https://api.sea-lion.ai/v1/chat/completions")
SEALION_KEY = os.environ.get("SEALION_API_KEY", "")
SEALION_MODEL = os.environ.get("SEALION_MODEL", "aisingapore/Gemma-SEA-LION-v4-27B-IT")


def _providers():
    """Ordered chain. The first that returns usable text wins."""
    chain = []
    if API_KEY or "freellmapi" in GATEWAY:
        chain.append(("freellmapi", GATEWAY, API_KEY, MODEL))
    if SEALION_KEY:
        chain.append(("sea-lion", SEALION_URL, SEALION_KEY, SEALION_MODEL))
    return chain

SYSTEM = """ທ່ານແມ່ນຜູ້ຊ່ວຍທາງການແພດທີ່ສະຫຼຸບ "ການປະເມີນລວມ" ຄວາມສ່ຽງເປັນອຳມະພາດ ເປັນພາສາລາວ.

ກົດລະບຽບເຂັ້ມງວດ:
1. ຫ້າມຄິດໄລ່ ຫຼື ສ້າງຕົວເລກໃໝ່ເດັດຂາດ. ໃຫ້ໃຊ້ສະເພາະຕົວເລກທີ່ໄດ້ຮັບມາເທົ່ານັ້ນ.
2. ຫ້າມກ່າວເຖິງເປີເຊັນອື່ນນອກຈາກທີ່ໃຫ້ມາ. ຖ້າບໍ່ມີເປີເຊັນໃຫ້ມາ, ຫ້າມກ່າວເຖິງເປີເຊັນໃດໆເລີຍ.
3. ຫ້າມວິນິດໄສ ແລະ ຫ້າມສັ່ງຢາ.
4. ຕອບເປັນພາສາລາວ, 4–6 ປະໂຫຍກ.

ໃຫ້ຄອບຄຸມທັງໝົດນີ້ (ພາບລວມ):
- ລະດັບຄວາມສ່ຽງລວມ ແລະ ຈຳນວນປັດໄຈສ່ຽງທີ່ພົບ.
- ສັນຍານຊີບທີ່ວັດໄດ້ (ຖ້າມີ) ວ່າຢູ່ໃນເກນປົກກະຕິ ຫຼື ບໍ່.
- ປັດໄຈໃດເປັນຕົວຂັບເຄື່ອນຄວາມສ່ຽງ ແລະ ປັດໄຈໃດທີ່ແກ້ໄຂໄດ້.
- ຄຳແນະນຳຕໍ່ໄປ ແລະ ຈົບດ້ວຍການແນະນຳໃຫ້ປຶກສາແພດ."""


class ExplainError(RuntimeError):
    pass


def _allowed_numbers(result, vitals=None):
    """Every numeric string the model is permitted to echo.

    This must include the measured vitals, not just the score: SpO2 is itself a
    percentage, and a model correctly reporting "96.2%" was being rejected as an
    invention. The guard exists to catch fabricated RISK figures, not to forbid
    the model from repeating a measurement it was handed.
    """
    allowed = {str(result.get("score")), str(result.get("max_score"))}
    for value in (vitals or {}).values():
        if value is None:
            continue
        for token in re.findall(r"\d+(?:\.\d+)?", str(value)):
            allowed.add(token)
            if token.endswith(".0"):
                allowed.add(token[:-2])
    pct = result.get("annual_risk_percent")
    if pct is not None:
        allowed |= {str(pct), str(int(pct)) if float(pct).is_integer() else str(pct),
                    f"{float(pct):.1f}"}
    for c in result.get("breakdown", []):
        allowed.add(str(c.get("points")))
    for k in ("burden_count", "burden_total"):
        if result.get(k) is not None:
            allowed.add(str(result[k]))
    return {a for a in allowed if a and a != "None"}


def _verify(text, result, vitals=None):
    """Reject any percentage neither the scorer nor the measurements produced."""
    allowed = _allowed_numbers(result, vitals)
    for raw in re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:%|ເປີເຊັນ)", text):
        if raw.replace(",", ".") not in allowed:
            raise ExplainError(f"model produced an unsourced percentage: {raw}%")
    return text


def _call(url, key, model, messages, max_tokens=420):
    """One OpenAI-compatible chat call. Returns the assistant text."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise ExplainError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ExplainError(f"unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ExplainError(f"invalid JSON: {exc}") from exc

    try:
        choice = payload["choices"][0]
        text = (choice["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ExplainError(f"unexpected response shape: {exc}") from exc

    # A reasoning model can spend the whole budget on hidden reasoning and
    # return empty content with finish_reason "length" (observed on
    # Qwen-SEA-LION-v4.5). That is a failure, not an answer.
    if not text:
        if choice.get("finish_reason") == "length":
            raise ExplainError("model exhausted its token budget before answering")
        raise ExplainError("model returned an empty message")

    return text, payload.get("model", model)


def explain(result, vitals=None):
    """Return a Lao explanation of ``result``, trying each provider in turn.

    Raises ExplainError only when every provider failed; the message then names
    what each one did, so a failure is diagnosable without opening the logs.
    """
    facts = {
        "overall_band": result.get("overall_band"),
        "overall_label": result.get("overall_label"),
        "risk_factors_found": result.get("burden_count"),
        "risk_factors_checked": result.get("burden_total"),
        "factors_present": [c["label"] for c in result.get("breakdown", [])],
        "modifiable_factors": result.get("modifiable_factors", []),
    }
    # The percentage is included only when one genuinely exists, so the model is
    # never holding a number it might be tempted to reshape.
    if result.get("annual_risk_percent") is not None:
        facts.update({
            "instrument": result.get("instrument"),
            "score": result.get("score"),
            "max_score": result.get("max_score"),
            "annual_risk_percent": result.get("annual_risk_percent"),
            "risk_band": result.get("risk_band"),
        })
    else:
        facts["percentage_available"] = False
        facts["why_no_percentage"] = result.get("reason")

    if vitals:
        facts["measured_vitals"] = {k: v for k, v in {
            "heart_rate_bpm": vitals.get("heart_rate"),
            "respiratory_rate_per_min": vitals.get("respiratory_rate"),
            "spo2_percent": vitals.get("spo2"),
            "blood_pressure": vitals.get("blood_pressure"),
        }.items() if v is not None}

    user = ("ຂໍ້ມູນຜົນການປະເມີນ (ຫ້າມປ່ຽນຕົວເລກ):\n"
            + json.dumps(facts, ensure_ascii=False, indent=1)
            + "\n\nຈົ່ງອະທິບາຍຜົນນີ້ເປັນພາສາລາວ.")

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]

    chain = _providers()
    if not chain:
        raise ExplainError("no explanation provider configured")

    failures = []
    for name, url, key, model in chain:
        try:
            text, reported = _call(url, key, model, messages)
            # The numeric guard runs per provider: a backup is not trusted more
            # than the primary, and a model that invents a figure is skipped
            # rather than allowed through because it answered last.
            return {"text": _verify(text, result, vitals),
                    "model": reported,
                    "provider": name,
                    "fallback_used": name != chain[0][0],
                    "failures": failures}
        except ExplainError as exc:
            failures.append(f"{name}: {exc}")

    raise ExplainError("all providers failed — " + "; ".join(failures))
