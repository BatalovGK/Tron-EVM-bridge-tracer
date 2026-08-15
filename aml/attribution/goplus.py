# attribution/goplus.py
"""
GoPlus Security — Malicious Address API (агрегирует SlowMist + BlockSec + OFAC
+ Chainabuse, готовые risk-флаги, раздел 4 архитектуры).

Endpoint подтверждён официальной документацией (docs.gopluslabs.io):
    GET https://api.gopluslabs.io/api/v1/address_security/{address}?chain_id=...
    Заголовок Authorization: Bearer <token> — опционален (выше лимиты с ним),
    в отличие от Blockscout/Etherscan публичный тир не требует обязательной
    регистрации ключа на момент подготовки (июль 2026).

В отличие от evm_adapter (bulk-трейсинг, требует полноценный token-bucket
rate limiter), это лёгкий per-адрес запрос для Attribution Engine — здесь
используется упрощённый клиент с обычным retry на 429, без token bucket.
"""

import logging
from typing import Any, Dict, Optional

import aiohttp

from common.secrets import get_secret

logger = logging.getLogger(__name__)

GOPLUS_BASE_URL = "https://api.gopluslabs.io/api/v1/address_security"

# Поля ответа GoPlus - каждое "1" означает подтверждённый риск-флаг (подтверждено
# по официальному примеру ответа в документации/SDK gopluslabs-api).
_RISK_FIELDS_TO_LABEL_TYPE = {
    "phishing_activities": "phishing",
    "blackmail_activities": "blackmail",
    "stealing_attack": "stealing",
    "fake_kyc": "fake_kyc",
    "malicious_mining_activities": "malicious_mining",
    "darkweb_transactions": "darkweb",
    "cybercrime": "cybercrime",
    "money_laundering": "money_laundering",
    "financial_crime": "financial_crime",
    "blacklist_doubt": "blacklisted",
    "honeypot_related_address": "honeypot_related",
}


async def check_address_goplus(address: str, chain_id: int) -> Dict[str, Any]:
    """
    Запрашивает GoPlus Malicious Address API по одному адресу.

    Returns:
        {
            "raw": <сырой ответ API>,
            "risk_flags": ["phishing", "money_laundering", ...],  # только сработавшие
            "is_risky": bool,
        }
    """
    api_key = get_secret("GOPLUS_API_KEY", required=False)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    url = f"{GOPLUS_BASE_URL}/{address}"
    params = {"chain_id": str(chain_id)}

    timeout = aiohttp.ClientTimeout(total=10)
    retries = 0
    max_retries = 3

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while True:
            async with session.get(url, params=params) as response:
                if response.status == 429 and retries < max_retries:
                    retries += 1
                    continue
                response.raise_for_status()
                data = await response.json()
                break

    # GoPlus обычно оборачивает полезные данные в {"code":1,"message":"OK","result":{...}}
    result = data.get("result", data) if isinstance(data, dict) else data
    if not isinstance(result, dict):
        result = {}

    risk_flags = [
        label for field, label in _RISK_FIELDS_TO_LABEL_TYPE.items()
        if str(result.get(field, "0")) == "1"
    ]

    return {
        "raw": data,
        "risk_flags": risk_flags,
        "is_risky": len(risk_flags) > 0,
    }
