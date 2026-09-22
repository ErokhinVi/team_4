"""Блок cib — корпоратив и бизнес-логика банка команды.

Каталог продуктов и логика кредитного решения (POST /credit/decide).
За данными клиента ходит в backend по BACKEND_URL.
Хелпер src/llm.py — для человеческого объяснения решения.
"""
from __future__ import annotations

import os
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

TEAM_NAME = os.environ.get("TEAM_NAME", "team")
COMMIT = os.environ.get("RENDER_GIT_COMMIT", "local")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8003").rstrip("/")

CREDIT = {
    "id": "credit-consumer", "kind": "credit", "name": "Потребительский кредит",
    "rate_pct": 19.9, "min_amount_rub": 50_000, "max_amount_rub": 3_000_000,
    "min_term_months": 6, "max_term_months": 60,
}
MAX_PAYMENT_SHARE = 0.4  # monthly payment may take at most 40% of income
MAX_RISK_SCORE = 0.5

PRODUCTS = [
    {"id": "card-debit", "kind": "card", "name": "Дебетовая карта", "segment": "mass"},
    {"id": "deposit-base", "kind": "deposit", "name": "Срочный депозит", "rate_pct": 14.0},
    CREDIT,
]

app = FastAPI(title="cib — корпоратив и бизнес-логика", version="1.0.0")


class CreditRequest(BaseModel):
    client_id: str
    amount_rub: int = Field(gt=0)
    term_months: int = Field(
        default=12, ge=CREDIT["min_term_months"], le=CREDIT["max_term_months"]
    )


def _rub(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def _monthly_payment(amount: float, rate_pct: float, months: int) -> float:
    """Annuity payment: equal monthly installments."""
    r = rate_pct / 100 / 12
    return amount * r / (1 - (1 + r) ** -months)


async def _fetch_client(client_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.get(f"{BACKEND_URL}/clients/{quote(client_id, safe='')}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"backend не ответил: {exc}") from exc
    if resp.status_code == 404:
        raise HTTPException(404, f"Клиент {client_id} не найден")
    if resp.status_code != 200:
        raise HTTPException(502, f"backend вернул {resp.status_code}")
    return resp.json()


async def _decide(req: CreditRequest) -> dict:
    client = await _fetch_client(req.client_id)
    rate = CREDIT["rate_pct"]
    rate_text = f"{rate:g}".replace(".", ",")
    min_amount, max_amount = CREDIT["min_amount_rub"], CREDIT["max_amount_rub"]

    income = client.get("income_rub") or 0
    affordable = income * MAX_PAYMENT_SHARE / _monthly_payment(1, rate, req.term_months)
    amount = min(req.amount_rub, max_amount, int(affordable // 1000 * 1000))

    if client.get("has_overdue_history"):
        refusal = "в кредитной истории есть просрочки"
    elif (client.get("risk_score") or 0) > MAX_RISK_SCORE:
        refusal = "высокий кредитный риск"
    elif req.amount_rub < min_amount:
        refusal = f"минимальная сумма кредита {_rub(min_amount)}"
    elif amount < min_amount:
        refusal = f"доход не позволяет взять даже {_rub(min_amount)} на {req.term_months} мес."
    else:
        refusal = None

    result = {
        "client_id": req.client_id, "client_name": client.get("name"),
        "product_id": CREDIT["id"], "requested_amount_rub": req.amount_rub,
        "term_months": req.term_months, "rate_pct": rate,
    }
    if refusal:
        return {**result, "decision": "declined", "approved_amount_rub": 0,
                "monthly_payment_rub": 0, "reason": f"Отказ: {refusal}."}

    payment = round(_monthly_payment(amount, rate, req.term_months))
    reason = (f"Одобрено {_rub(amount)} на {req.term_months} мес. под {rate_text}%, "
              f"платеж {_rub(payment)} в месяц.")
    if amount < req.amount_rub:
        limit = ("это максимум по продукту" if amount == max_amount
                 else "платеж не должен превышать 40% дохода")
        reason += f" Просили {_rub(req.amount_rub)}, но {limit}."
    return {**result, "decision": "approved", "approved_amount_rub": amount,
            "monthly_payment_rub": payment, "reason": reason}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "team": TEAM_NAME, "block": "cib",
            "commit": COMMIT, "backend_url": BACKEND_URL, "products": len(PRODUCTS)}


@app.get("/products")
async def products() -> dict:
    return {"total": len(PRODUCTS), "items": PRODUCTS}


@app.post("/credit/decide")
async def credit_decide(req: CreditRequest) -> dict:
    return await _decide(req)


@app.get("/credit/decide")
async def credit_decide_get(req: Annotated[CreditRequest, Query()]) -> dict:
    """Same decision via query string, handy for a browser address bar."""
    return await _decide(req)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    rows = "".join(
        f"<tr><td>{p['id']}</td><td>{p['kind']}</td><td>{p['name']}</td></tr>"
        for p in PRODUCTS
    )
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<title>cib · Райффайзен</title><style>"
        "body{font-family:system-ui;background:#0c0d10;color:#e8e9ec;padding:32px}"
        "h1{font-weight:500}table{border-collapse:collapse;margin-top:16px}"
        "td,th{border:1px solid #23262f;padding:8px 14px;text-align:left}"
        "</style></head><body>"
        "<h1>cib — корпоратив и бизнес-логика</h1>"
        f"<p>Команда: {TEAM_NAME}. Каталог продуктов:</p>"
        f"<table><tr><th>id</th><th>вид</th><th>название</th></tr>{rows}</table>"
        "</body></html>"
    )
