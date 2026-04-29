from datetime import datetime, timezone
from supabase_client import get_supabase_client
from .blockchain import daily_checkin_blockchain

DAILY_REWARD = 0.01
WEEKLY_BONUS = 0.1


class DailyCheckinManager:
    def __init__(self):
        self.supabase = get_supabase_client()

    def _today_utc(self):
        return datetime.now(timezone.utc).date()

    def _state(self, wallet):
        res = self.supabase.table("daily_checkin_state").select("*").eq("wallet_address", wallet).limit(1).execute()
        if res.data:
            return res.data[0]
        row = {
            "wallet_address": wallet,
            "current_streak": 0,
            "last_checkin_date_utc": None,
            "total_daily_rewards": 0,
            "total_weekly_bonus_sent": 0,
        }
        self.supabase.table("daily_checkin_state").insert(row).execute()
        return row

    def get_status(self, wallet):
        state = self._state(wallet)
        today = self._today_utc().isoformat()
        return {
            "success": True,
            "current_streak": int(state.get("current_streak") or 0),
            "last_checkin_date_utc": state.get("last_checkin_date_utc"),
            "can_checkin": state.get("last_checkin_date_utc") != today,
            "daily_reward": DAILY_REWARD,
            "weekly_bonus": WEEKLY_BONUS,
        }

    def checkin(self, wallet):
        state = self._state(wallet)
        today = self._today_utc()
        last_raw = state.get("last_checkin_date_utc")
        if last_raw == today.isoformat():
            return {"success": False, "error": "Already checked in today (UTC)"}

        streak = int(state.get("current_streak") or 0)
        streak_before = streak
        if last_raw:
            last_date = datetime.fromisoformat(last_raw).date()
            streak = streak + 1 if (today - last_date).days == 1 else 1
        else:
            streak = 1

        updates = {
            "current_streak": streak,
            "last_checkin_date_utc": today.isoformat(),
            "total_daily_rewards": float(state.get("total_daily_rewards") or 0) + DAILY_REWARD,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.supabase.table("daily_checkin_state").update(updates).eq("wallet_address", wallet).execute()
        self.supabase.table("daily_checkin_history").insert({
            "wallet_address": wallet,
            "event_type": "daily_checkin",
            "amount_celo": DAILY_REWARD,
            "streak_before": streak_before,
            "streak_after": streak,
            "status": "success",
        }).execute()

        bonus = None
        if streak >= 7:
            payout = daily_checkin_blockchain.send_celo(wallet, WEEKLY_BONUS)
            bonus_status = "success" if payout.get("success") else "failed"
            self.supabase.table("daily_checkin_history").insert({
                "wallet_address": wallet,
                "event_type": "weekly_bonus_auto_sent" if payout.get("success") else "weekly_bonus_failed",
                "amount_celo": WEEKLY_BONUS,
                "streak_before": streak,
                "streak_after": 0,
                "tx_hash": payout.get("tx_hash"),
                "status": bonus_status,
            }).execute()
            total_bonus = float(state.get("total_weekly_bonus_sent") or 0) + (WEEKLY_BONUS if payout.get("success") else 0)
            self.supabase.table("daily_checkin_state").update({
                "current_streak": 0,
                "total_weekly_bonus_sent": total_bonus,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("wallet_address", wallet).execute()
            bonus = payout

        return {
            "success": True,
            "current_streak": 0 if streak >= 7 else streak,
            "weekly_bonus_result": bonus,
        }

    def history(self, wallet, limit=20):
        res = self.supabase.table("daily_checkin_history").select("*").eq("wallet_address", wallet).order("created_at", desc=True).limit(limit).execute()
        return {"success": True, "history": res.data or []}


daily_checkin_manager = DailyCheckinManager()
