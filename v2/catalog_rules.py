"""Shared catalog eligibility rules for V2 matching and review."""

import math
from typing import Any, Dict, Optional


ITEM_STATUS_FIELDS = (
    "Katalog: Item Status",
    "Item Status",
    "item_status",
)
ALC_FIELDS = ("Katalog: ALC", "ALC", "alc", "Alc")


def _first_value(data: Dict[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        if field in data and data[field] is not None:
            return data[field]
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() == "nan" else text


def _positive_number(value: Any) -> Optional[float]:
    text = _text(value).replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def catalog_product_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return normalized status, ALC and the V2 eligibility decision."""
    item_status = _text(
        data.get("item_status")
        if "item_status" in data
        else _first_value(data, ITEM_STATUS_FIELDS)
    )
    alc = _positive_number(
        data.get("alc") if "alc" in data else _first_value(data, ALC_FIELDS)
    )

    errors = []
    if item_status.casefold() != "sellable":
        errors.append(
            f"Item Status er {item_status or 'ikke angitt'}, ikke Sellable"
        )
    if alc is None:
        errors.append("ALC må være større enn 0")

    return {
        "item_status": item_status,
        "alc": alc,
        "eligible": not errors,
        "masterdata_error": ". ".join(errors),
    }


def annotate_candidate(
    candidate: Dict[str, Any],
    catalog_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    annotated = dict(candidate)
    if catalog_row is not None:
        if "item_status" in annotated:
            annotated["persisted_item_status"] = annotated["item_status"]
        if "alc" in annotated:
            annotated["persisted_alc"] = annotated["alc"]
        metadata_source = dict(catalog_row)
    else:
        metadata_source = {
            key: annotated[key]
            for key in ("item_status", "alc")
            if key in annotated
        }
    annotated.update(catalog_product_metadata(metadata_source))
    return annotated
