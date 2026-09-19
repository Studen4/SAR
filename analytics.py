class AnalyticsEngine:
    @staticmethod
    def calc_ratio(num: float, den: float) -> float:
        if den == 0:
            return float(num) if num > 0 else 0.0
        return round(num / den, 2)

    @staticmethod
    def get_color_style(val: float) -> str:
        if val < 0.8:
            return "color: #ff4b4b; font-weight: bold;"
        elif 0.8 <= val <= 1.5:
            return "color: #00c853; font-weight: bold;"
        else:
            return "color: #00e5ff; font-weight: bold;"

    @staticmethod
    def get_tp_diff_style(val: float) -> str:
        if val > 0:
            return "color: #00c853; font-weight: bold;"
        elif val < 0:
            return "color: #ff4b4b; font-weight: bold;"
        return "color: #888888;"

    @staticmethod
    def calc_tp(s: dict) -> float:
        if not s:
            return 0.0
        K, A = s.get("kills", 0), s.get("assists", 0)
        D = max(s.get("deaths", 0), 1)
        DD = s.get("damage_dealt", 0)
        DR = max(s.get("damage_recv", 0), 1)
        HS, BS, LS = s.get("headshots", 0), s.get("bodyshots", 0), s.get("limbshots", 0)

        total = HS + BS + LS
        R1 = max(((K + A) / D) / 1.1, 0.0001)
        R2 = max((K / D) / 0.8, 0.0001)
        R3 = max((DD / DR) / 1.0, 0.0001)

        if total > 0:
            ch, cb, cl = 2.0, 1.0, 0.5
            baseline = ch * 0.3 + cb * 0.5 + cl * 0.2
            R4 = max((ch * HS + cb * BS + cl * LS) / total / baseline, 0.0001)
        else:
            R4 = 1.0

        w1, w2, w3, w4 = 0.35, 0.15, 0.30, 0.20
        tp = round(100 * (R1 ** w1) * (R2 ** w2) * (R3 ** w3) * (R4 ** w4), 1)
        return tp

    @classmethod
    def get_tier_letter(cls, tp: float) -> str:
        if tp > 150:
            return "S"
        elif 140 <= tp <= 150:
            return "A"
        elif 130 <= tp < 140:
            return "B"
        elif 120 <= tp < 130:
            return "C"
        elif 110 <= tp < 120:
            return "D"
        elif 90 <= tp < 110:
            return "E"
        else:
            return "F"

    @classmethod
    def compute_cw_metrics(cls, start_stats: dict, end_stats: dict) -> dict:
        d_kills = max(0, end_stats.get("kills", 0) - start_stats.get("kills", 0))
        d_assists = max(0, end_stats.get("assists", 0) - start_stats.get("assists", 0))
        d_deaths = max(0, end_stats.get("deaths", 0) - start_stats.get("deaths", 0))

        kd = cls.calc_ratio(d_kills, d_deaths)
        k_a_d = cls.calc_ratio(d_kills + d_assists, d_deaths)

        d_grenades = max(0, end_stats.get("grenades", 0) - start_stats.get("grenades", 0))

        d_head = max(0, end_stats.get("headshots", 0) - start_stats.get("headshots", 0))
        d_body = max(0, end_stats.get("bodyshots", 0) - start_stats.get("bodyshots", 0))
        d_limb = max(0, end_stats.get("limbshots", 0) - start_stats.get("limbshots", 0))
        total_shots = d_head + d_body + d_limb

        acc_head = cls.calc_ratio(d_head * 100, total_shots)
        acc_body = cls.calc_ratio(d_body * 100, total_shots)
        acc_limb = cls.calc_ratio(d_limb * 100, total_shots)

        d_dmg_dealt = max(0, end_stats.get("damage_dealt", 0) - start_stats.get("damage_dealt", 0))
        d_dmg_recv = max(0, end_stats.get("damage_recv", 0) - start_stats.get("damage_recv", 0))
        un_up = cls.calc_ratio(d_dmg_dealt, d_dmg_recv)

        delta_stats = {
            "nickname": end_stats.get("nickname", "Unknown"),
            "kills": d_kills,
            "assists": d_assists,
            "deaths": d_deaths,
            "damage_dealt": d_dmg_dealt,
            "damage_recv": d_dmg_recv,
            "headshots": d_head,
            "bodyshots": d_body,
            "limbshots": d_limb
        }
        slice_tp = cls.calc_tp(delta_stats)
        total_tp = cls.calc_tp(end_stats)

        tp_diff_pct = round(((slice_tp - total_tp) / total_tp) * 100, 1) if total_tp > 0 else 0.0

        return {
            "nickname": end_stats.get("nickname", "Unknown"),
            "cw_score": f"{d_kills} / {d_deaths} / {d_assists}",
            "slice_tp": slice_tp,
            "total_tp": total_tp,
            "tp_diff_pct": tp_diff_pct,
            "kd": kd,
            "kad": k_a_d,
            "grenades": d_grenades,
            "shots": f"Г: {d_head} | Т: {d_body} | К: {d_limb}",
            "accuracy": f"🎯 {acc_head}% / 🧍 {acc_body}% / 🦵 {acc_limb}%",
            "damage": f"+{round(d_dmg_dealt, 1)} / -{round(d_dmg_recv, 1)}",
            "un_up": un_up
        }