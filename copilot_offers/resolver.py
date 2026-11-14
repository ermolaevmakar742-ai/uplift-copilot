"""The ONE canonical resolver: account_ref -> assigned program -> validated OfferContext. Typed result, read-only, no network.

Runtime-facing: callers get an OfferResolution (status, reason, program_id, context), never DB rows."""
from copilot_programs.service import ProgramService

from .models import OfferReason, OfferResolution, OfferStatus
from .service import OfferService
from .validate import ACCOUNT_REF, validate_offer
from .context import build_offer_context


def resolve_from_data(account_ref, assigned_program_id, program, content) -> OfferResolution:
    """Pure core (no I/O): the same rules for runtime and tests."""
    if not isinstance(account_ref, str) or not ACCOUNT_REF.fullmatch(account_ref):
        return OfferResolution(OfferStatus.ACCOUNT_UNKNOWN, OfferReason.ACCOUNT_UNKNOWN)
    if not assigned_program_id:
        return OfferResolution(OfferStatus.NO_OFFER_ASSIGNED, OfferReason.NO_ASSIGNMENT)
    pid = assigned_program_id
    if program is None:
        return OfferResolution(OfferStatus.OFFER_UNAVAILABLE, OfferReason.PROGRAM_NOT_FOUND, pid)
    if not program.active:
        return OfferResolution(OfferStatus.OFFER_UNAVAILABLE, OfferReason.INACTIVE, pid)
    ctx = None if validate_offer(program, content) is not None else build_offer_context(account_ref, program, content)
    if ctx is None:
        return OfferResolution(OfferStatus.OFFER_UNAVAILABLE, OfferReason.INVALID, pid)
    return OfferResolution(OfferStatus.OFFER_ASSIGNED, OfferReason.OFFER_ASSIGNED, pid, ctx)


async def resolve_assigned_offer(session_factory, account_ref) -> OfferResolution:
    svc = OfferService(session_factory)
    pid = await svc.get_assignment(account_ref) if isinstance(account_ref, str) and ACCOUNT_REF.fullmatch(account_ref) else None
    program = await ProgramService(session_factory).get_program(pid) if pid else None
    content = await svc.get_content(pid) if program is not None and program.active else None
    return resolve_from_data(account_ref, pid, program, content)
