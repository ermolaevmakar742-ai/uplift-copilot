"""OfferContext builder. Used ONLY by the runtime resolver (shadow layer). NOT imported by main.py, the LLM path or the Telegram path.

Offer data is DATA. This builder copies only whitelisted structured fields; terms / notes / support_contact are never copied.
`description` / `brand` / `promo_code` are partner/admin free text: a future consumer must pass them as quoted, untrusted data and
never concatenate them into system instructions. The URL always comes from here, never from a model. Nothing is fetched."""
from .models import OfferContext, OfferImage
from .validate import validate_offer


def build_offer_context(account_ref: str, program, content: dict | None) -> OfferContext | None:
    """program: copilot_programs Program; content: OfferService.get_content() result. None when inactive or failing validation."""
    if program is None or not getattr(program, "active", False) or validate_offer(program, content) is not None:
        return None
    content = content or {}
    imgs, seen = [], set()
    for i in sorted(content.get("images") or [], key=lambda i: (i["sort_order"], i["image_id"])):
        if i.get("active", True) and i["url"] not in seen:        # validated, deduplicated, order preserved
            seen.add(i["url"])
            imgs.append(OfferImage(i["image_id"], i["url"], int(i["sort_order"]), True))
    return OfferContext(offer_id=program.program_id, account_ref=account_ref, name=program.name.strip(), brand=program.brand.strip(),
                        description=content.get("description"), registration_url=program.registration_url,
                        promo_code=program.promo_code, images=tuple(imgs), active=True)
