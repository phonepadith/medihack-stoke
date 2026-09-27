"""Stroke risk scoring.

WHAT IS AND IS NOT IMPLEMENTED HERE
-----------------------------------
`CHA2DS2-VASc` is implemented exactly. Its point structure is unambiguous and
universally agreed, so the score this module returns is the real score, not an
approximation. It is valid **only for patients with atrial fibrillation** --
that is the population it was derived and validated in -- and this module
refuses to report a percentage for anyone else rather than applying it out of
scope.

The Framingham Stroke Risk Profile (Wolf/D'Agostino, Stroke 1991;22:312-318)
would be the right instrument for a 10-year risk in a general population, and
`framingham_available()` is where it belongs. It is deliberately NOT
implemented: the published point tables are paywalled and could not be
obtained, and inventing them would produce a number carrying the authority of a
validated instrument without being one. That is the exact failure this whole
module exists to avoid. Supply the tables and it drops straight in.

EVERY patient gets an overall assessment. Atrial fibrillation is not a gate: it
only decides whether a validated PERCENTAGE can be attached. Without AF the
module reports risk-factor burden -- how many established stroke risk factors
are present, and which of them are modifiable -- which is informational and
defensible. With AF it additionally reports the CHA2DS2-VASc annual percentage.
What it never does is attach a probability to a population the instrument was
not derived in.
"""

# CHA2DS2-VASc components. Age contributes at one band only, never both.
COMPONENTS = [
    ("chf",        1, "ຫົວໃຈວາຍ / ການທຳງານຂອງຫົວໃຈຫ້ອງລຸ່ມຊ້າຍຜິດປົກກະຕິ", True),
    ("hypertension", 1, "ຄວາມດັນເລືອດສູງ", True),
    ("age_75",     2, "ອາຍຸ 75 ປີ ຂຶ້ນໄປ", False),
    ("diabetes",   1, "ພະຍາດເບົາຫວານ", True),
    ("prior_stroke", 2, "ເຄີຍເປັນ ອຳມະພາດ / TIA / ລິ່ມເລືອດອຸດຕັນ", False),
    ("vascular",   1, "ພະຍາດຫຼອດເລືອດ (ເຄີຍເປັນ MI, ຫຼອດເລືອດແຂນຂາ, ແຜ່ນໄຂມັນເສັ້ນເລືອດແດງໃຫຍ່)", True),
    ("age_65_74",  1, "ອາຍຸ 65–74 ປີ", False),
    ("female",     1, "ເພດຍິງ", False),
]

# Adjusted annual ischaemic stroke rate by score. Source: Michigan Medicine,
# "Inpatient Management of Acute Atrial Fibrillation and Atrial Flutter in
# Non-Pregnant Hospitalized Adults" (2021), NCBI Bookshelf NBK579414, table 2.
# Scores 7 and 8 are NOT monotonic with 6 in the source cohort -- those strata
# hold few patients and their confidence intervals are wide. Reported as
# published rather than smoothed, because smoothing would misrepresent it.
ANNUAL_RISK = {0: 0.0, 1: 1.3, 2: 2.2, 3: 3.2, 4: 4.0,
               5: 6.7, 6: 9.8, 7: 9.6, 8: 6.7, 9: 15.2}
NON_MONOTONIC_SCORES = {7, 8}

MODIFIABLE = {"hypertension", "diabetes", "chf", "vascular", "smoking"}

# Established stroke risk factors, counted for the overall burden summary. This
# is a count of recognised risk factors, NOT a probability -- deliberately
# qualitative, because turning a count into a percentage is precisely the step
# that has no evidence behind it outside a derived model.
BURDEN_FACTORS = ["hypertension", "diabetes", "smoking", "atrial_fibrillation",
                  "prior_stroke", "vascular", "chf", "age_65_plus"]

BURDEN_BANDS = [
    (0, 0, "low",          "ບໍ່ພົບປັດໄຈສ່ຽງທີ່ສຳຄັນ"),
    (1, 2, "moderate",     "ພົບປັດໄຈສ່ຽງບາງຢ່າງ"),
    (3, 99, "high",        "ພົບປັດໄຈສ່ຽງຫຼາຍຢ່າງ"),
]


def _burden(flags, age):
    """Count established stroke risk factors present. Qualitative by design."""
    present = [k for k in BURDEN_FACTORS if k != "age_65_plus" and flags.get(k)]
    if age is not None and age >= 65:
        present.append("age_65_plus")
    n = len(present)
    for lo, hi, band, label in BURDEN_BANDS:
        if lo <= n <= hi:
            return n, band, label
    return n, "high", BURDEN_BANDS[-1][3]


def framingham_available():
    """The 10-year general-population score is not implemented -- see module
    docstring. Kept explicit so callers can surface the gap honestly."""
    return False


def _resolve_age(age):
    """Return which single age band applies, if any."""
    if age is None:
        return None
    if age >= 75:
        return "age_75"
    if age >= 65:
        return "age_65_74"
    return None


def score(*, age=None, female=False, atrial_fibrillation=False,
          hypertension=False, diabetes=False, chf=False, vascular=False,
          prior_stroke=False, smoking=False):
    """Compute CHA2DS2-VASc when in scope; otherwise a risk-factor profile."""
    age_band = _resolve_age(age)
    present = {
        "chf": bool(chf),
        "hypertension": bool(hypertension),
        "age_75": age_band == "age_75",
        "diabetes": bool(diabetes),
        "prior_stroke": bool(prior_stroke),
        "vascular": bool(vascular),
        "age_65_74": age_band == "age_65_74",
        "female": bool(female),
    }

    breakdown, total = [], 0
    for key, points, label, modifiable in COMPONENTS:
        if present[key]:
            total += points
            breakdown.append({"key": key, "points": points,
                              "label": label, "modifiable": modifiable})

    # Smoking is not a CHA2DS2-VASc component but is a major modifiable stroke
    # risk factor, so it is carried in the profile without touching the score.
    extra = []
    if smoking:
        extra.append({"key": "smoking", "points": 0,
                      "label": "ສູບຢາ", "modifiable": True})

    modifiable_present = [c["label"] for c in breakdown + extra if c["modifiable"]]

    result = {
        "instrument": "CHA2DS2-VASc",
        "in_scope": bool(atrial_fibrillation),
        "score": total,
        "max_score": 9,
        "breakdown": breakdown + extra,
        "modifiable_factors": modifiable_present,
        "framingham_available": framingham_available(),
    }

    # Overall assessment, produced for every patient regardless of AF.
    n_burden, burden_band, burden_label = _burden({
        "hypertension": hypertension, "diabetes": diabetes, "smoking": smoking,
        "atrial_fibrillation": atrial_fibrillation, "prior_stroke": prior_stroke,
        "vascular": vascular, "chf": chf,
    }, age)
    result.update({
        "burden_count": n_burden,
        "burden_total": len(BURDEN_FACTORS),
        "overall_band": burden_band,
        "overall_label": burden_label,
    })

    if not atrial_fibrillation:
        # No validated percentage exists outside AF, so none is given -- but the
        # overall assessment above still stands and is the headline result.
        result.update({
            "annual_risk_percent": None,
            "percent_basis": None,
            "reason": "ບໍ່ມີ AF — ບໍ່ມີສູດທີ່ຜ່ານການກວດສອບສຳລັບການແປງເປັນເປີເຊັນ; "
                      "ລາຍງານເປັນການປະເມີນລວມແທນ",
            "risk_band": None,
        })
        return result

    risk = ANNUAL_RISK.get(min(total, 9))
    # Bands follow the usual anticoagulation decision thresholds, which are
    # sex-specific: a woman's single "female" point alone is not treated as risk.
    effective = total - (1 if present["female"] else 0)
    if effective == 0:
        band = "low"
    elif effective == 1:
        band = "intermediate"
    else:
        band = "high"

    result.update({
        "annual_risk_percent": risk,
        "percent_basis": "CHA2DS2-VASc (AF)",
        "risk_band": band,
        "rate_uncertain": min(total, 9) in NON_MONOTONIC_SCORES,
        "source": "Michigan Medicine 2021, NCBI Bookshelf NBK579414 table 2",
    })
    return result
