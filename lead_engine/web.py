"""Minimal operator web UI for the approval queue (thin layer over the app).

This is NOT a new approval path. Approve/reject here call exactly the same
functions the CLI calls - ``approval.human_gate.approve_lead`` / ``reject_lead``
- so there is zero decision logic in the web layer. The draft is shown read-only
(never editable in this MVP). Design constraints, deliberately kept small:

  * No new heavy dependency: plain ``HTMLResponse`` f-strings, no template
    engine, no form library. ``POST`` bodies are read as
    ``application/x-www-form-urlencoded`` via ``urllib.parse`` so we do NOT pull
    in ``python-multipart`` (which FastAPI's ``Form`` would otherwise require).
  * Every interpolated value - including untrusted raw source payloads - is
    HTML-escaped, so the page can never echo a lead's text as live markup (XSS).
  * No fake authentication. Identity is a plain ``actor-id`` text field forwarded
    to ``approve_lead(actor=...)`` for the audit trail only. The app binds to
    127.0.0.1 with no TLS: local, synthetic demo only (see README).
  * Tenant isolation is structural: the tenant is an explicit URL segment
    (``/ui/{tenant_id}/...``) and each request opens a tenant-scoped repository,
    so a page can only ever list or act on that one tenant's leads.
"""

from __future__ import annotations

import html
import json
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .approval import human_gate
from .audit import explain_lead
from .config import TENANTS, get_tenant
from .reporting import render_dashboard
from .metrics import funnel
from .service import build_service

router = APIRouter(prefix="/ui", tags=["operator-ui"])

_CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 24px;
       background: #fafafa; color: #222; }
h1 { margin: 0 0 4px 0; font-size: 22px; }
h2 { font-size: 16px; margin: 22px 0 6px; }
.sub { color: #666; font-size: 13px; margin-bottom: 16px; }
nav a { margin-right: 14px; font-size: 13px; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: 13px;
        box-shadow: 0 1px 0 rgba(0,0,0,0.02); }
th, td { border: 1px solid #e2e2e2; padding: 7px 9px; text-align: left;
         vertical-align: top; }
th { background: #f0f0f0; }
.card { background: #fff; border: 1px solid #e2e2e2; border-radius: 8px;
        padding: 14px 16px; margin: 12px 0; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; font-size: 13px; }
.kv b { color: #444; font-weight: 600; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
         color: #fff; margin-left: 6px; }
.red { background: #c54b4b; } .amber { background: #d59a2b; } .green { background: #35a35a; }
.blue { background: #4a86d4; } .grey { background: #8f8f8f; }
.draft { white-space: pre-wrap; background: #f7f7f7; border: 1px solid #e2e2e2;
         border-radius: 6px; padding: 10px 12px; font-size: 13px; }
.quarantine { border-color: #c54b4b; background: #fdf3f3; }
pre.raw { background: #f2f2f2; border-radius: 6px; padding: 10px; overflow-x: auto;
          font-size: 12px; }
form.inline { display: inline-block; margin-right: 10px; }
input[type=text] { padding: 6px 8px; border: 1px solid #ccc; border-radius: 5px;
                   font-size: 13px; width: 220px; }
button { padding: 7px 14px; border: 0; border-radius: 5px; font-size: 13px;
         cursor: pointer; color: #fff; }
.approve { background: #35a35a; } .reject { background: #c54b4b; }
.error { background: #fdf3f3; border: 1px solid #c54b4b; color: #7a2626;
         padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
.note { color: #666; font-size: 12px; }
"""


def e(value: object) -> str:
    """HTML-escape any value for safe interpolation (untrusted text included)."""
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{e(title)}</title><style>{_CSS}</style></head><body>"
        "<h1>AthenAI Lead Engine &mdash; интерфейс оператора</h1>"
        "<div class='sub'>Локально, офлайн, только синтетические данные. Без "
        "аутентификации/TLS &mdash; привязка только к 127.0.0.1. Этот UI переиспользует "
        "тот же шлюз одобрения, что и CLI.</div>"
        f"<nav><a href='/ui'>&#9662; тенанты</a></nav><hr>{body}</body></html>"
    )


def _open_tenant_nav(tenant_id: str) -> str:
    return (
        f"<nav><a href='/ui'>&#9662; тенанты</a>"
        f"<a href='/ui/{e(tenant_id)}/queue'>очередь</a>"
        f"<a href='/ui/{e(tenant_id)}/metrics'>метрики</a></nav>"
    )


def _require_tenant(tenant_id: str) -> None:
    try:
        get_tenant(tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="неизвестный тенант")


def _flag_badge(label: str, on: object) -> str:
    if int(on or 0):
        return f"<span class='badge red'>{e(label)}</span>"
    return ""


def _translate_gate_error(msg: str) -> str:
    """Human-readable Russian label for a known gate error, with the original
    English kept in parentheses so tests and the audit trail still match it."""
    if "actor is required" in msg:
        return (
            "\u041d\u0443\u0436\u0435\u043d \u043d\u0435\u043f\u0443\u0441\u0442\u043e\u0439 "
            "actor \u2014 \u0440\u0435\u0448\u0435\u043d\u0438\u0435 \u0434\u043e\u043b\u0436\u043d\u043e "
            "\u0431\u044b\u0442\u044c \u0430\u0443\u0434\u0438\u0440\u0443\u0435\u043c\u044b\u043c ("
            "actor is required)"
        )
    if "not found" in msg:
        return "\u041b\u0438\u0434 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d \u0432 \u044d\u0442\u043e\u043c \u0442\u0435\u043d\u0430\u043d\u0442\u0435 (not found)"
    if "already approved" in msg:
        return "\u041b\u0438\u0434 \u0443\u0436\u0435 \u043e\u0434\u043e\u0431\u0440\u0435\u043d (already approved)"
    if "opt-out" in msg:
        return "\u041b\u0438\u0434\u044b \u0441 \u043e\u0442\u043a\u0430\u0437\u043e\u043c (opt-out) \u043d\u0435\u043b\u044c\u0437\u044f \u043e\u0434\u043e\u0431\u0440\u044f\u0442\u044c (opt-out)"
    if "cannot approve" in msg or "cannot reject" in msg:
        return (
            "\u041d\u0435\u043b\u044c\u0437\u044f \u043f\u0440\u0438\u043d\u044f\u0442\u044c \u0440\u0435\u0448\u0435\u043d\u0438\u0435 "
            "\u0434\u043b\u044f \u043b\u0438\u0434\u0430 \u0432 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0441\u0442\u0430\u0434\u0438\u0438 "
            f"({msg})"
        )
    return msg


def _stage_badge(stage: str) -> str:
    color = {
        "manual_review": "amber", "approved_ready": "blue",
        "human_approved": "green", "human_rejected": "red", "rejected": "red",
        "outboxed": "green", "crm_synced": "green", "dlq": "red",
    }.get(stage, "grey")
    return f"<span class='badge {color}'>{e(stage)}</span>"


# ---------------------------------------------------------------------------
# GET /ui  - landing page: one link per tenant (no cross-tenant listing).
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def ui_index() -> HTMLResponse:
    rows = "".join(
        "<li><a href='/ui/{t}/queue'>{t}</a> &mdash; {name} "
        "<span class='note'>(владелец: {owner})</span></li>".format(
            t=e(tid), name=e(t.display_name), owner=e(t.owner)
        )
        for tid, t in TENANTS.items()
    )
    body = (
        "<h2>Тенанты</h2>"
        "<p class='sub'>Выберите тенант, чтобы открыть его изолированную очередь "
        "одобрения. Каждая страница отнесена ровно к одному тенанту.</p>"
        f"<ul>{rows}</ul>"
    )
    return HTMLResponse(_page("AthenAI \u2014 интерфейс оператора", body))


# ---------------------------------------------------------------------------
# GET /ui/{tenant}/queue  - table of pending manual_review / approved_ready.
# ---------------------------------------------------------------------------
@router.get("/{tenant_id}/queue", response_class=HTMLResponse)
def ui_queue(tenant_id: str) -> HTMLResponse:
    _require_tenant(tenant_id)
    svc = build_service(tenant_id)
    try:
        items = human_gate.pending_queue(svc.repo)
        rows: list[str] = []
        for item in items:
            lead = svc.repo.get_lead(item["lead_id"]) or {}
            flags = item.get("flags", {})
            flag_html = (
                _flag_badge("опт-аут", flags.get("opt_out"))
                + _flag_badge("инъекция", flags.get("injection"))
                + _flag_badge("конфликт", flags.get("conflict"))
            ) or "<span class='note'>&mdash;</span>"
            rows.append(
                "<tr>"
                f"<td><code>{e(item['lead_id'])}</code></td>"
                f"<td>{e(lead.get('canonical_key'))}</td>"
                f"<td>{e(lead.get('rules_verdict'))}</td>"
                f"<td>{e(lead.get('ai_verdict'))}</td>"
                f"<td>{e(lead.get('ai_confidence'))}</td>"
                f"<td>{e(item.get('combined_verdict'))}</td>"
                f"<td>{flag_html}</td>"
                f"<td>{e(lead.get('updated_at'))}</td>"
                f"<td><a href='/ui/{e(tenant_id)}/lead/{e(item['lead_id'])}'>Открыть</a></td>"
                "</tr>"
            )
        table = (
            "<table><thead><tr>"
            "<th>ид лида</th><th>канонический ключ</th><th>вердикт правил</th>"
            "<th>вердикт ИИ</th><th>уверенность</th><th>итог</th>"
            "<th>флаги безопасности</th><th>обновлён</th><th></th>"
            "</tr></thead><tbody>"
            + ("".join(rows) or
               "<tr><td colspan='9' class='note'>Нет лидов, ожидающих решения.</td></tr>")
            + "</tbody></table>"
        )
        body = (
            f"{_open_tenant_nav(tenant_id)}"
            f"<h2>{e(tenant_id)} &mdash; очередь на одобрение {_stage_badge('manual_review')} "
            f"<span class='note'>ожидают решения человека</span></h2>"
            f"{table}"
        )
        return HTMLResponse(_page(f"{tenant_id} \u2014 очередь", body))
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# GET /ui/{tenant}/lead/{lead_id}  - full read-only detail + decision form.
# ---------------------------------------------------------------------------
@router.get("/{tenant_id}/lead/{lead_id}", response_class=HTMLResponse)
def ui_lead_detail(tenant_id: str, lead_id: str) -> HTMLResponse:
    _require_tenant(tenant_id)
    svc = build_service(tenant_id)
    try:
        try:
            view = explain_lead(svc.repo, svc.crm, svc.outbox, lead_id)
        except human_gate.ApprovalError:
            raise HTTPException(status_code=404, detail="лид не найден в тенанте")

        idn = view["identity"]
        nf = view["normalize_flags"]
        rules = view["rules"]
        ai = view["ai"]

        sources_html = ""
        for s in view["sources"]:
            raw_json = json.dumps(s.get("raw"), ensure_ascii=False, indent=2, sort_keys=True)
            sources_html += (
                "<div class='card'>"
                f"<div class='kv'><b>источник</b><span>{e(s.get('source_type'))}</span>"
                f"<b>ключ идемпотентности</b><span><code>{e(s.get('idempotency_key'))}</code></span>"
                f"<b>получен</b><span>{e(s.get('received_at'))}</span></div>"
                f"<pre class='raw'>{e(raw_json)}</pre></div>"
            )
        if not sources_html:
            sources_html = "<p class='note'>Нет сырых записей источников.</p>"

        flags_html = (
            _flag_badge("опт-аут", nf.get("opt_out"))
            + _flag_badge("инъекция", nf.get("injection"))
            + _flag_badge("конфликт", nf.get("conflict"))
            + _flag_badge("нужна проверка", nf.get("needs_review"))
        ) or "<span class='note'>нет</span>"

        safety_flags = ai.get("reason_or_safety_flags") or ""
        safety_html = (
            f"<span class='badge red'>безопасность: {e(safety_flags)}</span>"
            if ai.get("safety_flagged") else "<span class='note'>ok</span>"
        )

        draft = view.get("draft")
        if draft is None:
            draft_html = "<p class='note'>Для этого лида черновик не сгенерирован.</p>"
        else:
            body_text = draft.get("body") or ""
            is_quarantined = body_text.startswith("[Quarantined draft]")
            cls = "draft quarantine" if is_quarantined else "draft"
            quarantine_note = (
                "<div class='note' style='color:#7a2626'>&#9888; Карантин: лид "
                "помечен для проверки безопасности; текст сообщения не был "
                "сгенерирован из недоверенного ввода.</div>"
                if is_quarantined else ""
            )
            ev = ", ".join(draft.get("evidence_field_ids") or [])
            draft_html = (
                "<p class='note'>Только для чтения. Сгенерирован только из "
                "доказательств (ручное редактирование в этом UI недоступно).</p>"
                f"<div class='kv'><b>статус</b><span>{e(draft.get('status'))}</span>"
                f"<b>тема</b><span>{e(draft.get('subject'))}</span>"
                f"<b>evidence_field_ids</b><span>{e(ev) or '&mdash;'}</span></div>"
                f"{quarantine_note}"
                f"<div class='{cls}'>{e(body_text)}</div>"
            )

        decisions_html = "".join(
            "<li><b>{a}</b> &mdash; <code>{actor}</code> · {note} "
            "<span class='note'>({ts})</span></li>".format(
                a=e(d.get("action")), actor=e(d.get("actor")),
                note=e(d.get("note") or "&mdash;"), ts=e(d.get("created_at")),
            )
            for d in view["decisions"]
        ) or "<li class='note'>Решений пока нет.</li>"

        can_act = view["stage"] in ("manual_review", "approved_ready")
        if can_act:
            form_html = f"""
            <h2>Принять решение</h2>
            <p class='note'>Поле <b>actor</b> &mdash; это обычный идентификатор, который
            передаётся в тот же шлюз одобрения, что и CLI (пишется в аудит,
            аутентификации нет). Поля для произвольного текста сообщения
            намеренно нет: отправить можно только сохранённый черновик.</p>
            <form class='inline' method='post'
                  action='/ui/{e(tenant_id)}/lead/{e(lead_id)}/approve'>
              <input type='text' name='actor' placeholder='ваш actor-id (напр. ops-alpha)' required>
              <input type='text' name='note' placeholder='примечание (необязательно)'>
              <button class='approve' type='submit'>Одобрить</button>
            </form>
            <form class='inline' method='post'
                  action='/ui/{e(tenant_id)}/lead/{e(lead_id)}/reject'>
              <input type='text' name='actor' placeholder='ваш actor-id' required>
              <input type='text' name='note' placeholder='примечание (необязательно)'>
              <button class='reject' type='submit'>Отклонить</button>
            </form>
            """
        else:
            form_html = (
                f"<h2>Принять решение</h2><p class='note'>Этот лид находится в стадии "
                f"{_stage_badge(view['stage'])} и больше не принимает одобрение/отклонение "
                f"из очереди.</p>"
            )

        body = f"""
        {_open_tenant_nav(tenant_id)}
        <h2>Лид <code>{e(lead_id)}</code> {_stage_badge(view['stage'])}</h2>
        <div class='card'><div class='kv'>
          <b>тенант</b><span>{e(view['tenant_id'])}</span>
          <b>канонический ключ</b><span>{e(view['canonical_key'])}</span>
          <b>стадия</b><span>{e(view['stage'])}</span>
          <b>итоговый вердикт</b><span>{e(view['combined_verdict'])}</span>
        </div></div>

        <h2>Контакт (нормализованный)</h2>
        <div class='card'><div class='kv'>
          <b>email</b><span>{e(idn.get('email'))}</span>
          <b>телефон</b><span>{e(idn.get('phone'))}</span>
          <b>имя</b><span>{e(idn.get('name'))}</span>
          <b>компания</b><span>{e(idn.get('company'))}</span>
          <b>страна</b><span>{e(idn.get('country'))}</span>
          <b>бюджет</b><span>{e(idn.get('budget_band'))}</span>
          <b>уровень намерения</b><span>{e(idn.get('intent_level'))}</span>
        </div></div>

        <h2>Флаги нормализации</h2>
        <div class='card'>{flags_html}</div>

        <h2>Вердикт правил</h2>
        <div class='card'><div class='kv'>
          <b>вердикт</b><span>{e(rules.get('verdict'))}</span>
          <b>причина</b><span>{e(rules.get('reason'))}</span>
        </div></div>

        <h2>Вердикт ИИ</h2>
        <div class='card'><div class='kv'>
          <b>вердикт</b><span>{e(ai.get('verdict'))}</span>
          <b>уверенность</b><span>{e(ai.get('confidence'))}</span>
          <b>флаги безопасности</b><span>{safety_html}</span>
        </div></div>

        <h2>Черновик (только для чтения)</h2>
        <div class='card'>{draft_html}</div>

        <h2>Записи источников (сырые)</h2>
        {sources_html}

        <h2>История решений</h2>
        <ul>{decisions_html}</ul>

        {form_html}
        """
        return HTMLResponse(_page(f"{tenant_id} \u2014 лид {lead_id}", body))
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# GET /ui/{tenant}/metrics  - reuse the CLI report dashboard generator.
# ---------------------------------------------------------------------------
@router.get("/{tenant_id}/metrics", response_class=HTMLResponse)
def ui_metrics(tenant_id: str) -> HTMLResponse:
    _require_tenant(tenant_id)
    svc = build_service(tenant_id)
    try:
        metrics = funnel.compute(svc.repo, svc.crm, svc.outbox)
    finally:
        svc.close()
    dash = render_dashboard([metrics])
    nav = (
        "<div style='margin:0 0 16px'>"
        f"<a href='/ui'>&#9662; тенанты</a> &middot; "
        f"<a href='/ui/{e(tenant_id)}/queue'>очередь</a> &middot; "
        f"<a href='/ui/{e(tenant_id)}/metrics'>метрики (эта страница)</a>"
        "<div class='sub'>Ярлыки ниже переиспользуются из отчёта `cli report` "
        "и потому на английском.</div></div>"
    )
    # Inject a small back-navigation bar just after <body> of the standalone
    # dashboard, so the reused generator stays untouched.
    dash = dash.replace("<body>", "<body>" + nav, 1)
    return HTMLResponse(dash)


# ---------------------------------------------------------------------------
# POST /ui/{tenant}/lead/{lead_id}/{approve|reject}
#   Form body: actor, note (urlencoded). NO message/text field by design - only
#   the stored draft can ever be sent. Calls the same human_gate functions CLI
#   uses; ApprovalError maps to a clear HTTP status, then redirect back to queue.
# ---------------------------------------------------------------------------
async def _read_form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def _error_page(status: int, message: str, tenant_id: str, lead_id: str) -> HTMLResponse:
    """Return a clear HTTP error (status) that a browser can also read, with a
    link back to the queue. Used for rejected decisions (no-actor / wrong stage /
    cross-tenant lead) so the endpoint never silently fails."""
    body = (
        f"{_open_tenant_nav(tenant_id)}"
        f"<div class='error'>&#9888; {e(message)}</div>"
        f"<p class='note'>HTTP {status} &mdash; лид не изменён.</p>"
        f"<p><a href='/ui/{e(tenant_id)}/lead/{e(lead_id)}'>К лиду</a> &middot; "
        f"<a href='/ui/{e(tenant_id)}/queue'>К очереди</a></p>"
    )
    return HTMLResponse(_page(f"Ошибка решения ({status})", body), status_code=status)


def _apply_decision(tenant_id: str, lead_id: str, actor: str, note: str, kind: str) -> HTMLResponse | RedirectResponse:
    svc = build_service(tenant_id)
    try:
        gate = human_gate.approve_lead if kind == "approve" else human_gate.reject_lead
        try:
            gate(svc.repo, lead_id, actor=actor, note=note)
        except human_gate.ApprovalError as exc:
            msg = str(exc)
            # Map the shared domain error onto a clear HTTP status. "not found"
            # is only raised for a genuinely missing lead (the actor invariant is
            # checked first inside the gate), so this stays unambiguous.
            if "not found" in msg:
                status = 404
            elif "actor is required" in msg:
                status = 400
            else:
                status = 409
            return _error_page(status, _translate_gate_error(msg), tenant_id, lead_id)
    finally:
        svc.close()
    return RedirectResponse(url=f"/ui/{tenant_id}/queue", status_code=303)


@router.post("/{tenant_id}/lead/{lead_id}/approve", response_class=HTMLResponse)
async def ui_approve(tenant_id: str, lead_id: str, request: Request):
    _require_tenant(tenant_id)
    form = await _read_form(request)
    actor = (form.get("actor") or "").strip()
    note = form.get("note") or ""
    if not actor:
        # 400 with the same message the gate raises; the lead is left untouched.
        return _error_page(400, _translate_gate_error("actor is required"), tenant_id, lead_id)
    return _apply_decision(tenant_id, lead_id, actor, note, "approve")


@router.post("/{tenant_id}/lead/{lead_id}/reject", response_class=HTMLResponse)
async def ui_reject(tenant_id: str, lead_id: str, request: Request):
    _require_tenant(tenant_id)
    form = await _read_form(request)
    actor = (form.get("actor") or "").strip()
    note = form.get("note") or ""
    if not actor:
        return _error_page(400, _translate_gate_error("actor is required"), tenant_id, lead_id)
    return _apply_decision(tenant_id, lead_id, actor, note, "reject")
