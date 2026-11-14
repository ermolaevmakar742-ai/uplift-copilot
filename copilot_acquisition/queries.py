"""READ-ONLY acquisition analytics (SQL-side). Never writes, never creates tables.

Cohort = attribution_clicks.clicked_at. A lead (users.client_id) belongs to the cohort of its FIRST-touch click
(acquisition_leads.first_click_id, else the legacy users.attribution_click_id). Downstream events (qualified / registration / deposit /
reward) are counted per UNIQUE lead, once. Reward = shadow_decisions.reward with the LAST delayed correction applied; never
attribution_clicks.revenue. Output is aggregate only: no Telegram id, client id, username or outcome id leaves this module.

Qualified = the lead's first funnel_events transition into `offer` or any later stage of the existing production funnel vocabulary
(FUNNEL_STAGES: new, conversation, qualified, offer, click, registration, deposit, converted + the postback stages that follow offer).
(There is no stage called `propuesta` in the code; `offer` is the existing proposal stage.) No new state is stored.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

MATURITY_DAYS = 30
SMALL_SAMPLE = 30
QUALIFIED_STAGES = ("offer", "click", "action", "registration_started", "registered", "registration", "card_added",
                    "deposit_attempt", "deposit", "deposited", "converted", "withdraw_request", "withdrawn")
DIMENSIONS = {"source": "src", "campaign": "camp", "creative": "cre", "medium": "med"}      # allowlist: user input never reaches SQL text
PERIODS = ("today", "7d", "30d", "all")
UNKNOWN = "Unknown"
# Placeholders for tables that do not exist yet. Time/number columns are typed: an untyped NULL is `text` on PostgreSQL,
# and COALESCE(text, timestamptz) in the CTEs below fails (seen on LIVE while acquisition_leads is absent).
_TS = "CAST(NULL AS TIMESTAMP WITH TIME ZONE)"
_EMPTY = {
    "acquisition_campaigns": "SELECT NULL AS campaign_id, NULL AS source, NULL AS medium, NULL AS campaign_name, NULL AS creative_id WHERE 1=0",
    "acquisition_leads": f"SELECT NULL AS lead_ref, NULL AS first_click_id, {_TS} AS first_touch_at WHERE 1=0",
    "funnel_events": f"SELECT NULL AS user_id, NULL AS new_stage, {_TS} AS created_at WHERE 1=0",
    "outcomes": f"SELECT NULL AS chat_id, NULL AS outcome_type, {_TS} AS observed_at WHERE 1=0",
    "shadow_decisions": ("SELECT NULL AS chat_id, CAST(NULL AS DOUBLE PRECISION) AS reward, NULL AS reward_version, NULL AS reward_detail, "
                         f"NULL AS reward_corrections, {_TS} AS outcome_timestamp, {_TS} AS outcome_closed_at WHERE 1=0"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime, dialect: str):
    """PG timestamptz wants an aware value, SQLite compares text: pass naive UTC there."""
    return dt if dialect != "sqlite" else dt.astimezone(timezone.utc).replace(tzinfo=None)


def period_start(period: str, now: datetime):
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    return None


def _src(name: str, tables: set) -> str:
    return name if name in tables else f"({_EMPTY[name]})"


def _label(expr: str) -> str:
    return f"COALESCE(NULLIF(TRIM({expr}), ''), '{UNKNOWN}')"


def _reward_exprs(dialect: str) -> tuple:
    if dialect == "sqlite":
        eff = "COALESCE(json_extract(sd.reward_corrections, '$[#-1].corrected_reward'), sd.reward)"
        fin = "json_extract(sd.reward_detail, '$.status') = 'final'"
    else:
        eff = "COALESCE(((sd.reward_corrections -> -1) ->> 'corrected_reward')::float8, sd.reward)"
        fin = "(sd.reward_detail ->> 'status') = 'final'"
    return eff, fin


def _ctes(tables: set, dialect: str) -> str:
    eff, fin = _reward_exprs(dialect)
    return f"""
cd AS (
  SELECT ac.click_id, ac.clicked_at,
         {_label('COALESCE(cp.source, ac.source)')} AS src,
         {_label('COALESCE(cp.campaign_name, ac.campaign)')} AS camp,
         {_label('COALESCE(cp.creative_id, ac.creative)')} AS cre,
         {_label('cp.medium')} AS med
  FROM attribution_clicks ac
  LEFT JOIN {_src('acquisition_campaigns', tables)} cp ON cp.campaign_id = ac.campaign
),
ld AS (
  SELECT u.id AS chat_id, COALESCE(al.first_click_id, u.attribution_click_id) AS first_click_id,
         COALESCE(al.first_touch_at, fc.registered_at) AS lead_at
  FROM users u
  LEFT JOIN {_src('acquisition_leads', tables)} al ON al.lead_ref = u.client_id
  LEFT JOIN attribution_clicks fc ON fc.click_id = COALESCE(al.first_click_id, u.attribution_click_id)
  WHERE COALESCE(al.first_click_id, u.attribution_click_id) IS NOT NULL
),
lq AS (SELECT user_id AS chat_id, MIN(created_at) AS at FROM {_src('funnel_events', tables)}
       WHERE new_stage IN ({','.join("'" + s + "'" for s in QUALIFIED_STAGES)}) GROUP BY user_id),
lr AS (SELECT chat_id, MIN(observed_at) AS at FROM {_src('outcomes', tables)} WHERE outcome_type = 'REGISTRATION' GROUP BY chat_id),
lp AS (SELECT chat_id, MIN(observed_at) AS at FROM {_src('outcomes', tables)} WHERE outcome_type = 'DEPOSIT' GROUP BY chat_id),
dr AS (
  SELECT sd.chat_id, {eff} AS r, CASE WHEN {fin} OR sd.outcome_timestamp <= :mature_cut THEN 1 ELSE 0 END AS is_final,
         COALESCE(sd.outcome_timestamp, sd.outcome_closed_at) AS at
  FROM {_src('shadow_decisions', tables)} sd
  WHERE sd.reward_version IS NOT NULL AND {eff} IS NOT NULL
),
lw AS (SELECT chat_id, SUM(r) AS total, MIN(is_final) AS all_final FROM dr GROUP BY chat_id)
"""


async def _tables(sf) -> tuple:
    from sqlalchemy import inspect
    async with sf() as s:
        conn = await s.connection()
        names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        return names, conn.dialect.name


def _filters(f: dict, extra_params: dict) -> str:
    """Predicates on the click dimensions (labels). Values are BOUND parameters; the column names come from the allowlist."""
    out = []
    for dim, col in DIMENSIONS.items():
        v = f.get(dim)
        if v:
            out.append(f"cd.{col} = :f_{dim}")
            extra_params[f"f_{dim}"] = v
    return (" AND " + " AND ".join(out)) if out else ""


def _rate(a, b):
    return None if not b else round(a / b, 4)


def _row_from(r: dict) -> dict:
    leads, clicks = r["leads"] or 0, r["clicks"] or 0
    rn = r["reward_leads"] or 0
    return {
        "clicks": clicks, "leads": leads, "qualified": r["qualified"] or 0, "registrations": r["registrations"] or 0,
        "deposits": r["deposits"] or 0, "deposit_rate": _rate(r["deposits"] or 0, leads),
        # reward: None = unknown (no lead has a resolved reward); 0.0 = a real zero
        "reward": None if not rn else round(r["reward_total"], 6),
        "reward_leads": rn, "reward_coverage": _rate(rn, leads),
        "reward_per_lead": None if not rn else round(r["reward_total"] / rn, 6),
        "reward_per_click": None if (not rn or not clicks) else round(r["reward_total"] / clicks, 6),
        "final_30d": {"value": None if not r["final_leads"] else round(r["final_total"], 6), "leads": r["final_leads"] or 0},
        "provisional": {"value": None if not r["prov_leads"] else round(r["prov_total"], 6), "leads": r["prov_leads"] or 0},
        "immature_clicks": r["immature"] or 0, "mature": (r["immature"] or 0) == 0 and clicks > 0, "n": clicks,
        "small_sample": clicks < SMALL_SAMPLE,
    }


_AGG = """
  COUNT(DISTINCT cd.click_id) AS clicks,
  COUNT(DISTINCT l.chat_id) AS leads,
  COUNT(DISTINCT CASE WHEN q.at IS NOT NULL THEN l.chat_id END) AS qualified,
  COUNT(DISTINCT CASE WHEN rg.at IS NOT NULL THEN l.chat_id END) AS registrations,
  COUNT(DISTINCT CASE WHEN p.at IS NOT NULL THEN l.chat_id END) AS deposits,
  COUNT(DISTINCT CASE WHEN w.chat_id IS NOT NULL THEN l.chat_id END) AS reward_leads,
  COALESCE(SUM(w.total), 0) AS reward_total,
  COUNT(DISTINCT CASE WHEN w.all_final = 1 THEN l.chat_id END) AS final_leads,
  COALESCE(SUM(CASE WHEN w.all_final = 1 THEN w.total END), 0) AS final_total,
  COUNT(DISTINCT CASE WHEN w.all_final = 0 THEN l.chat_id END) AS prov_leads,
  COALESCE(SUM(CASE WHEN w.all_final = 0 THEN w.total END), 0) AS prov_total,
  SUM(CASE WHEN cd.clicked_at > :mature_cut THEN 1 ELSE 0 END) AS immature
"""
_JOINS = """FROM cd LEFT JOIN ld l ON l.first_click_id = cd.click_id LEFT JOIN lq q ON q.chat_id = l.chat_id
  LEFT JOIN lr rg ON rg.chat_id = l.chat_id LEFT JOIN lp p ON p.chat_id = l.chat_id LEFT JOIN lw w ON w.chat_id = l.chat_id"""


async def grouped(sf, *, group_by: str | None, period: str = "all", filters: dict | None = None, now: datetime | None = None) -> list:
    """Cohort aggregates, one row per `group_by` label (or one total row when None). Period applies to clicked_at."""
    if group_by is not None and group_by not in DIMENSIONS:
        raise ValueError("invalid group_by")
    if period not in PERIODS:
        raise ValueError("invalid period")
    now = now or _now()
    tables, dialect = await _tables(sf)
    params = {"mature_cut": _ts(now - timedelta(days=MATURITY_DAYS), dialect)}
    where = "WHERE 1=1" + _filters(filters or {}, params)
    start = period_start(period, now)
    if start is not None:
        where += " AND cd.clicked_at >= :p_start"
        params["p_start"] = _ts(start, dialect)
    sel = f"cd.{DIMENSIONS[group_by]} AS k, " if group_by else "'' AS k, "
    sql = f"WITH {_ctes(tables, dialect)} SELECT {sel}{_AGG} {_JOINS} {where}" + (f" GROUP BY cd.{DIMENSIONS[group_by]}" if group_by else "")
    async with sf() as s:                                   # SELECT only; the session is never committed
        rows = (await s.execute(text(sql), params)).mappings().all()
    out = [dict(_row_from(dict(r)), key=r["k"]) for r in rows if (r["clicks"] or 0) > 0 or group_by is None]
    out.sort(key=lambda x: (-x["clicks"], x["key"] == UNKNOWN, x["key"] or ""))
    return out


async def total_clicks(sf) -> int:
    async with sf() as s:
        return (await s.execute(text("SELECT COUNT(*) FROM attribution_clicks"))).scalar() or 0


async def filter_options(sf) -> dict:
    tables, dialect = await _tables(sf)
    params = {"mature_cut": _ts(_now() - timedelta(days=MATURITY_DAYS), dialect)}
    out = {}
    async with sf() as s:
        for dim, col in DIMENSIONS.items():
            sql = f"WITH {_ctes(tables, dialect)} SELECT cd.{col} AS v, COUNT(*) AS n FROM cd GROUP BY cd.{col} ORDER BY n DESC, v"
            out[dim] = [{"value": r[0], "clicks": r[1]} for r in (await s.execute(text(sql), params)).all()]
    return out


_DAY = {"sqlite": "strftime('%Y-%m-%d', {c})", "pg": "to_char({c} AT TIME ZONE 'UTC', 'YYYY-MM-DD')"}
_SERIES = {   # metric -> (event timestamp expression, value expression, extra joins)
    "clicks": ("cd.clicked_at", "COUNT(DISTINCT cd.click_id)", ""),
    "leads": ("l.lead_at", "COUNT(DISTINCT l.chat_id)", "JOIN ld l ON l.first_click_id = cd.click_id"),
    "deposits": ("p.at", "COUNT(DISTINCT l.chat_id)", "JOIN ld l ON l.first_click_id = cd.click_id JOIN lp p ON p.chat_id = l.chat_id"),
    "reward": ("d.at", "SUM(d.r)", "JOIN ld l ON l.first_click_id = cd.click_id JOIN dr d ON d.chat_id = l.chat_id"),
}


async def timeseries(sf, *, metric: str, period: str = "30d", filters: dict | None = None, now: datetime | None = None) -> dict:
    """Daily series of ONE metric by ITS OWN event timestamp (clicks: clicked_at, leads: bind time, deposits: first DEPOSIT outcome,
    reward: decision outcome time). The period is a window on that event time; source / campaign / creative filter the first-touch click."""
    if metric not in _SERIES or period not in PERIODS:
        raise ValueError("invalid metric or period")
    now = now or _now()
    tables, dialect = await _tables(sf)
    ts_expr, val, join = _SERIES[metric]
    params = {"mature_cut": _ts(now - timedelta(days=MATURITY_DAYS), dialect)}
    where = f"WHERE {ts_expr} IS NOT NULL" + _filters(filters or {}, params)
    start = period_start(period, now)
    if start is not None:
        where += f" AND {ts_expr} >= :p_start"
        params["p_start"] = _ts(start, dialect)
    day = _DAY["sqlite" if dialect == "sqlite" else "pg"].format(c=ts_expr)
    sql = f"WITH {_ctes(tables, dialect)} SELECT {day} AS day, {val} AS v FROM cd {join} {where} GROUP BY {day} ORDER BY day"
    async with sf() as s:
        pts = [{"day": r[0], "value": None if r[1] is None else round(float(r[1]), 6)} for r in (await s.execute(text(sql), params)).all()]
    return {"metric": metric, "period": period, "points": pts, "enough": len(pts) >= 2, "timestamp": {
        "clicks": "clicked_at", "leads": "first-touch bind time", "deposits": "first DEPOSIT outcome", "reward": "decision outcome time"}[metric]}


def funnel(kpi: dict) -> list:
    steps = [("clicks", kpi["clicks"]), ("leads", kpi["leads"]), ("qualified", kpi["qualified"]),
             ("registrations", kpi["registrations"]), ("deposits", kpi["deposits"])]
    out, prev = [], None
    for name, n in steps:
        out.append({"stage": name, "count": n, "conversion_from_previous": None if prev is None else _rate(n, prev)})
        prev = n
    return out
