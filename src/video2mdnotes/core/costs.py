"""Cost ledger and hard spend caps for metered LLM calls.

Why this exists: the failover chain can reach paid backends (an ordinary
subscription failure falls through; `--on-exhaustion metered` or
ALLOW_METERED_FALLBACK escalate on purpose). Without a ledger, "at most $10 on
OpenAI for this effort" is a hope, not a constraint. With it, every metered
call is checked BEFORE it is made, against what has already been spent across
every run that shared the same ledger file.

Three properties are load-bearing:

* **Check before calling, with a worst-case estimate.** The estimate is prompt
  tokens plus the full `max_tokens` output allowance, so a call that could
  overrun the cap is refused rather than discovered afterwards.
* **Subscription calls are never blocked.** `claude-cli/…` is recorded (usd=0)
  so the ledger counts them, but no cap applies — that capacity is already paid.
* **Fail closed.** An unreadable ledger means spend is unknown, so metered calls
  are refused (BudgetExhausted) instead of assumed free.

The ledger is a single JSON file shared by all runs pointed at it; delete or
re-point COST_LEDGER_PATH to start a new budget.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from video2mdnotes.config import settings

# Kinds of backend, as recorded in the ledger.
SUBSCRIPTION = "subscription"
METERED = "metered"
LOCAL = "local"

# One id per process, so a ledger can be read back per run. Overridable via
# settings.run_id (e.g. a checkpointed long-form run that resumes across
# processes and wants one id).
_PROCESS_RUN_ID = f"{dt.datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def current_run_id() -> str:
    return (settings.run_id or "").strip() or _PROCESS_RUN_ID


class BudgetExhausted(Exception):
    """A metered call was refused because it would exceed a spend cap.

    Distinct from SubscriptionExhausted and from ordinary failures: the
    failover loop moves on to the next metered provider, and when every one is
    capped the run stops cleanly (callers checkpoint; library code raises,
    never exits).
    """

    def __init__(self, message: str, provider: str | None = None,
                 spent: float = 0.0, cap: float | None = None,
                 estimate: float = 0.0):
        super().__init__(message)
        self.provider = provider
        self.spent = spent
        self.cap = cap
        self.estimate = estimate


class AnthropicApiRefused(RuntimeError):
    """An `anthropic/*` route was refused: the Anthropic API credit is reserved
    (advisor + final code review) and the pipeline must never spend it."""


def refuse_anthropic_api(model: str) -> None:
    """Raise AnthropicApiRefused for an anthropic/* model unless
    ALLOW_ANTHROPIC_API=true. Called BEFORE any ledger check or network call."""
    bare = (model or "").strip().lower()
    if bare.startswith("anthropic/") and not getattr(settings, "allow_anthropic_api", False):
        raise AnthropicApiRefused(
            f"{model}: anthropic/* routes are refused — the Anthropic API credit is "
            "reserved. Set ALLOW_ANTHROPIC_API=true to permit it deliberately.")


# --- Pricing ------------------------------------------------------------------

# USD per 1M tokens (input, output). Used only when litellm has no price for a
# model (e.g. Nous models routed through the generic openai/ adapter). Nous
# prices are from the Nous inference API listing as last checked — verify
# before relying on them for a tight cap.
PRICE_TABLE_PER_M: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "hermes-4-405b": (1.00, 3.00),
    "hermes-4-70b": (0.13, 0.40),
    "hermes-4.3-36b": (0.10, 0.40),
}
# Unknown models are priced high on purpose: an unpriced model must never be
# treated as cheap enough to slip under the cap.
DEFAULT_PRICE_PER_M: tuple[float, float] = (15.00, 75.00)


def _table_price(model: str) -> tuple[float, float]:
    bare = model.split("/", 1)[-1].strip().lower()
    return PRICE_TABLE_PER_M.get(bare, DEFAULT_PRICE_PER_M)


def price_tokens(model: str, input_tokens: int, output_tokens: int,
                 litellm_model: str | None = None) -> float:
    """USD for a token count: litellm's price map first, the table otherwise."""
    import litellm

    if litellm_model:
        try:
            cin, cout = litellm.cost_per_token(
                model=litellm_model,
                prompt_tokens=int(input_tokens),
                completion_tokens=int(output_tokens),
            )
            total = float(cin) + float(cout)
            if total > 0 or (input_tokens == 0 and output_tokens == 0):
                return total
        except Exception:  # noqa: BLE001 - unmapped model -> table
            pass
    pin, pout = _table_price(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def estimate_prompt_tokens(text: str, litellm_model: str | None = None) -> int:
    """Prompt token count; tokenizer when available, else a conservative chars/3."""
    if litellm_model:
        try:
            import litellm
            n = litellm.token_counter(
                model=litellm_model, messages=[{"role": "user", "content": text}]
            )
            if isinstance(n, int) and n > 0:
                return n
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 3)


def _as_int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_and_cost(response, model: str, litellm_model: str | None,
                   fallback_usd: float) -> tuple[int, int, float, bool]:
    """(input_tokens, output_tokens, usd, estimated) for a completed call.

    When the response carries no usable usage block, the pre-call worst-case
    estimate is charged instead — a ledger that under-counts is worse than one
    that over-counts.
    """
    usage = getattr(response, "usage", None)
    tin = _as_int(getattr(usage, "prompt_tokens", None))
    tout = _as_int(getattr(usage, "completion_tokens", None))
    if tin is None or tout is None:
        return 0, 0, float(fallback_usd), True

    usd = None
    try:
        import litellm
        value = litellm.completion_cost(completion_response=response)
        if isinstance(value, (int, float)) and value > 0:
            usd = float(value)
    except Exception:  # noqa: BLE001
        pass
    if usd is None:
        usd = price_tokens(model, tin, tout, litellm_model)
    return tin, tout, usd, False


# --- Ledger -------------------------------------------------------------------

class CostLedger:
    """Append-only JSON ledger of LLM calls, with spend totals and cap checks."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "entries": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError(f"cost ledger {self.path} is not a ledger")
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def entries(self) -> list[dict]:
        with self._locked():
            return list(self._read()["entries"])

    def spent(self, provider: str | None = None) -> float:
        """Total metered USD, optionally for one provider."""
        return sum(
            float(e.get("usd", 0.0)) for e in self.entries()
            if e.get("kind") == METERED and (provider is None or e.get("provider") == provider)
        )

    def calls(self, provider: str | None = None, kind: str | None = None) -> int:
        return sum(
            1 for e in self.entries()
            if (provider is None or e.get("provider") == provider)
            and (kind is None or e.get("kind") == kind)
        )

    def record(self, *, provider: str, model: str, kind: str,
               input_tokens: int, output_tokens: int, usd: float,
               run_id: str | None = None, estimated: bool = False) -> dict:
        entry = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id or current_run_id(),
            "provider": provider,
            "model": model,
            "kind": kind,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "usd": 0.0 if kind != METERED else round(float(usd), 6),
            "estimated": bool(estimated),
        }
        with self._locked():
            data = self._read()
            data["entries"].append(entry)
            self._write(data)
        return entry

    @staticmethod
    def _check_entries(entries: list[dict], provider: str, estimate_usd: float,
                       total_cap: float | None, provider_caps: dict[str, float] | None) -> None:
        metered = [e for e in entries if e.get("kind") == METERED]
        total = sum(float(e.get("usd", 0.0)) for e in metered)
        mine = sum(float(e.get("usd", 0.0)) for e in metered if e.get("provider") == provider)

        cap = (provider_caps or {}).get(provider)
        if cap is not None and mine + estimate_usd > float(cap):
            raise BudgetExhausted(
                f"{provider} cap ${float(cap):.2f} would be exceeded: spent ${mine:.4f}, "
                f"this call could cost up to ${estimate_usd:.4f}",
                provider=provider, spent=mine, cap=float(cap), estimate=estimate_usd,
            )
        if total_cap is not None and total + estimate_usd > float(total_cap):
            raise BudgetExhausted(
                f"metered cap ${float(total_cap):.2f} (all paid providers) would be "
                f"exceeded: spent ${total:.4f}, this call to {provider} could cost up "
                f"to ${estimate_usd:.4f}",
                provider=provider, spent=total, cap=float(total_cap), estimate=estimate_usd,
            )

    def check(self, provider: str, estimate_usd: float,
              total_cap: float | None, provider_caps: dict[str, float] | None) -> None:
        """Raise BudgetExhausted if a metered call of this estimate could overrun a cap.

        Read-only. A caller that goes on to make the call must use `reserve`
        instead: check-then-record is not atomic (three chapter threads
        would each pass against the same total).
        """
        try:
            entries = self.entries()
        except Exception as exc:  # noqa: BLE001 - unknown spend: fail closed
            raise BudgetExhausted(
                f"cost ledger unreadable ({exc}); refusing metered call to {provider}",
                provider=provider,
            ) from exc
        self._check_entries(entries, provider, estimate_usd, total_cap, provider_caps)

    def reserve(self, *, provider: str, model: str, estimate_usd: float,
                total_cap: float | None, provider_caps: dict[str, float] | None,
                input_tokens: int = 0, run_id: str | None = None) -> str:
        """Atomically check the caps AND book the worst-case estimate.

        Under one exclusive flock (serialises threads and processes alike):
        read, check, append a `reserved` entry charged at the estimate.
        `settle` later replaces it with the measured cost; a crash in between
        leaves the estimate charged, which errs toward stopping. Returns the
        reservation id.
        """
        rid = uuid.uuid4().hex
        with self._locked():
            try:
                data = self._read()
            except Exception as exc:  # noqa: BLE001 - unknown spend: fail closed
                raise BudgetExhausted(
                    f"cost ledger unreadable ({exc}); refusing metered call to {provider}",
                    provider=provider,
                ) from exc
            self._check_entries(data["entries"], provider, estimate_usd,
                                total_cap, provider_caps)
            data["entries"].append({
                "id": rid,
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                "run_id": run_id or current_run_id(),
                "provider": provider,
                "model": model,
                "kind": METERED,
                "input_tokens": int(input_tokens),
                "output_tokens": 0,
                "usd": round(float(estimate_usd), 6),
                "estimated": True,
                "reserved": True,
            })
            self._write(data)
        return rid

    def settle(self, reservation_id: str, *, input_tokens: int | None = None,
               output_tokens: int | None = None, usd: float | None = None,
               estimated: bool = True, note: str | None = None) -> dict | None:
        """Replace a reservation with the call's real (or estimated) cost.

        usd=None keeps the reserved estimate (a call that failed after it may
        have been billed). Returns the updated entry, or None if not found.
        """
        with self._locked():
            data = self._read()
            for e in data["entries"]:
                if e.get("id") == reservation_id:
                    if input_tokens is not None:
                        e["input_tokens"] = int(input_tokens)
                    if output_tokens is not None:
                        e["output_tokens"] = int(output_tokens)
                    if usd is not None:
                        e["usd"] = round(float(usd), 6)
                    e["estimated"] = bool(estimated)
                    e.pop("reserved", None)
                    if note:
                        e["note"] = note
                    self._write(data)
                    return dict(e)
        return None

    def release(self, reservation_id: str) -> None:
        """Drop a reservation for a call that was certainly not billed."""
        with self._locked():
            data = self._read()
            data["entries"] = [e for e in data["entries"] if e.get("id") != reservation_id]
            self._write(data)


def get_ledger() -> CostLedger:
    """The ledger configured in settings (resolved on every call, so tests and
    CLI overrides that change settings take effect)."""
    return CostLedger(settings.cost_ledger_path)
