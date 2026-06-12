"""
Self-test for the ladder logic — pure functions, no network.
Run: python selftest.py
Confirms the min-of-three-gates discipline behaves correctly across regimes.
"""
import signals as S


def scenario(name, funding, oi_change, fng, btc_price, regime):
    struct = S.structural_score(funding, oi_change)
    sent = S.sentiment_score(fng)
    confirms = S.confirmation_triggers(btc_price, funding, fng)
    ladder = S.compute_ladder(struct, sent, regime, confirms)
    n_conf = sum(confirms.values())
    print(f"{name:<42} regime={regime:<10} "
          f"struct={struct:5.1f} sent={sent:5.1f} "
          f"confirms={n_conf}/3 -> TARGET {ladder['target']:5.1f}%  "
          f"(raw {ladder['raw_desire']:.0f}, ceil {ladder['macro_ceiling']}, cap {ladder['confirm_cap']})")
    return ladder["target"]


if __name__ == "__main__":
    print("=" * 100)
    a = scenario("A) Calm + greed, hostile macro",        0.0005,  0.05, 65, 80000, "HOSTILE")
    b = scenario("B) Flush + extreme fear, 0 confirms",   -0.0005, -0.15, 13, 62000, "HOSTILE")
    c = scenario("C) Same flush, macro eased to NEUTRAL", -0.0005, -0.15, 13, 62000, "NEUTRAL")
    # D/E isolate the macro ceiling: high desire + enough confirms that the
    # confirmation cap is NOT the binding gate, so the macro ceiling shows through.
    d = scenario("D) Strong + 1 confirm, HOSTILE ",       -0.0005, -0.15, 35, 62000, "HOSTILE")
    e = scenario("E) Same, macro eased to SUPPORTIVE",    -0.0005, -0.15, 35, 62000, "SUPPORTIVE")
    print("=" * 100)

    # Sanity assertions — the correct mental model
    assert a < 20, "calm+greed should keep you light even with confirms"
    assert abs(b - 30) < 0.1, "0-confirm deep fear = 30% knife-catch tranche"
    assert abs(c - b) < 0.1, "easing macro alone must NOT lift you past the knife-catch cap"
    assert abs(d - 40) < 0.1, "hostile macro ceiling (40) binds once confirms unlock the cap"
    assert e > d, "easing macro to supportive lifts the ceiling -> more deployment"

    # ---- liquidation flush score (live WS layer) ----
    print("-" * 100)
    spent = S.liq_flush_score([0, 0, 100e6, 80e6, 30e6, 5e6])   # cascade decaying
    live = S.liq_flush_score([0, 0, 10e6, 40e6, 80e6, 120e6])   # still escalating
    none_ = S.liq_flush_score([0, 0, 0, 0, 0, 0])               # no cascade
    print(f"liq_flush  decaying-cascade={spent:.0f}  escalating={live:.0f}  none={none_:.0f}")
    assert spent > 90, "a cascade that's decayed to near zero = high flush score"
    assert live == 0, "a cascade still escalating (peak = now) must NOT signal exhaustion"
    assert none_ == 0, "no liquidations = no exhaustion signal"

    # ---- structural fallback when WS not ready ----
    with_ws = S.structural_score(-0.0005, -0.15, liq_flush=100)
    no_ws = S.structural_score(-0.0005, -0.15, liq_flush=None)
    print(f"structural  with-WS={with_ws:.1f}  WS-not-ready(fallback)={no_ws:.1f}")
    assert abs(no_ws - 100) < 0.1, "fallback must renormalize funding+OI cleanly (both maxed here)"

    # ---- v2: adaptive percentile scoring ----
    print("-" * 100)
    import baseline
    hist = [(-0.0005 + 0.00001 * i) for i in range(100)]   # range of funding values
    most_neg = baseline._pctile_low_is_bullish(-0.0005, hist)   # lowest -> bullish
    most_pos = baseline._pctile_low_is_bullish(0.0005, hist)    # highest -> bearish
    mid = baseline._pctile_low_is_bullish(hist[50], hist)
    print(f"percentile  most-negative={most_neg:.0f}  mid={mid:.0f}  most-positive={most_pos:.0f}")
    assert most_neg > 95 and most_pos < 5 and 40 < mid < 60, "percentile direction wrong"

    # ---- v2: scanner screens + breadth ----
    import scanner
    mock = [
        {"symbol": "AAAUSDT", "price": 1, "chg24h": -0.20, "funding": -0.002, "turnover": 9e6, "oi": 1},
        {"symbol": "BBBUSDT", "price": 1, "chg24h": 0.15, "funding": 0.003, "turnover": 8e6, "oi": 1},
        {"symbol": "CCCUSDT", "price": 1, "chg24h": -0.05, "funding": 0.0001, "turnover": 7e6, "oi": 1},
        {"symbol": "BTCUSDT", "price": 62000, "chg24h": -0.03, "funding": -0.0004, "turnover": 9e9, "oi": 1},
    ]
    sc = scanner._screens(mock)
    assert sc["funding_neg"][0]["symbol"] == "AAAUSDT", "most negative funding should rank first"
    assert sc["losers"][0]["symbol"] == "AAAUSDT", "biggest loser should rank first"
    assert sc["turnover"][0]["symbol"] == "BTCUSDT", "highest turnover should rank first"
    br = scanner.breadth(mock)
    print(f"scanner  losers[0]={sc['losers'][0]['symbol']}  funding_neg[0]={sc['funding_neg'][0]['symbol']}  breadth={br:.0f}%")
    assert br == 25.0, "1 of 4 green = 25% breadth"

    # ---- v2: position-aware sizing ----
    import sizing
    s_add = sizing.tranche(40, 10, capital=100000)
    s_hold = sizing.tranche(32, 30, capital=100000)
    print(f"sizing  add: {s_add['action']} ${s_add['delta_usd']:,.0f} BTC ${s_add['per_asset']['BTC']:,.0f} | "
          f"small-move actionable={s_hold['actionable']}")
    assert s_add["action"] == "ADD" and abs(s_add["delta_usd"] - 30000) < 1, "30pt gap on 100k = 30k"
    assert s_add["per_asset"]["BTC"] == 16500.0, "BTC 55% of 30k = 16.5k"
    assert s_hold["actionable"] is False, "2pt gap below MIN_TRANCHE_PCT should not be actionable"

    # ---- v2: store roundtrip ----
    import os, store as ST
    ST.C.DB_FILE = "/tmp/_ladder_test.db"
    if os.path.exists(ST.C.DB_FILE):
        os.remove(ST.C.DB_FILE)
    ST.init_db()
    ST.insert_reading({"funding": -0.0003, "oi_change": -0.1, "fng": 13, "btc_price": 62000,
                       "structural": 90, "sentiment": 87, "target": 30, "liq_flush": 80})
    vals = ST.recent_values("funding", 30)
    ST.set_meta("deployed_pct", 25)
    print(f"store  rows={len(vals)} funding[0]={vals[0]}  meta deployed_pct={ST.get_meta('deployed_pct')}")
    assert vals == [-0.0003] and ST.get_meta("deployed_pct") == "25", "store roundtrip failed"

    # ---- v3: trend math (pure, offline) ----
    print("-" * 100)
    import trend as T
    up_closes = [100.0 + i for i in range(200)]            # steady uptrend
    up_highs = [c + 1 for c in up_closes]
    up_lows = [c - 1 for c in up_closes]
    assert T.sma(up_closes, 50) == sum(up_closes[-50:]) / 50, "sma arithmetic"
    assert T.sma(up_closes, 500) is None, "sma needs enough data"
    flat = [100.0] * 200
    assert T.realized_vol(flat) == 0.0, "flat series has zero vol"
    choppy = [100.0 * (1.03 if i % 2 else 0.97) for i in range(200)]
    assert T.realized_vol(choppy) > 50, "choppy series has high vol"
    rp = T.range_position(up_highs, up_lows, up_closes[-1])
    assert rp is not None and rp > 90, "top of the range in an uptrend"
    ctx = T.build_context(up_closes, up_highs, up_lows, up_closes[-1])
    assert ctx["golden"] is True, "50d above 200d in a steady uptrend"
    assert ctx["px_vs_fast"] is not None and ctx["px_vs_fast"] > 0, "price above its 50d in an uptrend"
    assert ctx["reclaim_auto"] == ctx["sma_fast"], "auto reclaim level = 50d SMA"
    assert ctx["rv30"] is not None, "flat-zero vol must survive the None guards"
    print(f"trend  sma50={ctx['sma_fast']}  px_vs_50={ctx['px_vs_fast']:+.1f}%  "
          f"golden={ctx['golden']}  range_pos={ctx['range_pos']:.0f}  rv30={ctx['rv30']}")

    # ---- v3: overheat / top-risk mirror ----
    hot, hot_parts = S.overheat_score(0.0006, 85, 60.0, 0.08)
    calm, _ = S.overheat_score(-0.0003, 12, -20.0, -0.10)
    no_trend, nt_parts = S.overheat_score(0.0006, 85, None, 0.08)
    print(f"overheat  euphoric={hot:.0f} (parts {hot_parts})  capitulation={calm:.0f}  "
          f"no-trend-renorm={no_trend:.0f}")
    assert hot > 85, "rich funding + greed + stretched + ballooning OI = textbook top"
    assert calm < 5, "capitulation must not read as overheat"
    assert "extension" not in nt_parts and no_trend > 85, \
        "missing trend must renormalize, not drag the score down"

    # ---- v3: confirmation stability (funding debounce + dynamic reclaim) ----
    flicker = S.confirmation_triggers(80000, 0.0001, 50, None, -0.0002)
    assert flicker["funding_positive"] is False, "flicker above zero with negative avg must NOT confirm"
    stable = S.confirmation_triggers(80000, 0.0001, 50, None, 0.0001)
    assert stable["funding_positive"] is True, "current + avg positive = confirmed"
    legacy = S.confirmation_triggers(80000, 0.0001, 50)
    assert legacy["funding_positive"] is True, "no-avg call keeps the old instantaneous behavior"
    dyn = S.confirmation_triggers(60000, 0.0001, 50, 59000, None)
    assert dyn["btc_reclaim"] is True, "dynamic reclaim level must be honored"
    print("confirms  flicker-rejected=True  stable-accepted=True  dynamic-reclaim=True")

    # ---- v3: squeeze screen ----
    mock_sq = [
        {"symbol": "SQZUSDT", "price": 1, "chg24h": -0.30, "funding": -0.004, "turnover": 9e6, "oi": 1},
        {"symbol": "MEHUSDT", "price": 1, "chg24h": -0.06, "funding": -0.0002, "turnover": 8e6, "oi": 1},
        {"symbol": "UPPUSDT", "price": 1, "chg24h": -0.20, "funding": 0.001, "turnover": 8e6, "oi": 1},
        {"symbol": "OKUSDT", "price": 1, "chg24h": 0.10, "funding": -0.003, "turnover": 8e6, "oi": 1},
    ]
    sq = scanner._squeeze_rank(mock_sq)
    names = [r["symbol"] for r in sq]
    print(f"squeeze  ranked={names}")
    assert names and names[0] == "SQZUSDT", "most crowded-short loser ranks first"
    assert "UPPUSDT" not in names, "positive funding can't be a squeeze candidate"
    assert "OKUSDT" not in names, "a name that's up isn't a squeeze-into-weakness setup"

    # ---- v3: whale flow (pure + tape math, no network) ----
    import time as _time
    import whales as W
    book = W._book_imbalance(
        bids=[(100.0, 50), (99.5, 40), (97.0, 999)],   # 97.0 is outside ±2% of mid≈100
        asks=[(100.2, 10), (101.0, 10), (103.5, 999)], # 103.5 outside too
        pct=0.02)
    assert book is not None and book["bid_share"] > 0.8, "bid-heavy book must read bid-heavy"
    assert W._book_imbalance([], [], 0.02) is None, "empty book -> None"
    assert W.crowd_read(0.70) == "crowded long" and W.crowd_read(0.30) == "crowded short" \
        and W.crowd_read(0.50) == "balanced", "crowd labels"
    assert W.absorption_note(0.02, -0.01) is not None, "OI up + flat tape = absorption"
    assert W.absorption_note(0.02, -0.10) is None, "OI up into a dump is NOT absorption"
    assert W.absorption_note(-0.05, 0.0) is None, "OI down = no absorption read"
    tape = W.WhaleTapeMonitor(["BTCUSDT"], "", max_age_sec=14400, min_clip_usd=100000)
    now_ms = _time.time() * 1000
    tape.events.append((now_ms - 30 * 60e3, "Buy", 2_000_000))    # 30 min ago
    tape.events.append((now_ms - 10 * 60e3, "Sell", 500_000))     # 10 min ago
    tape.events.append((now_ms - 3 * 3600e3, "Buy", 9_000_000))   # 3h ago (outside 1h)
    d1, d4 = tape.delta(3600), tape.delta(14400)
    print(f"whales  book_bid={book['bid_share']:.2f}  crowd(0.70)={W.crowd_read(0.70)}  "
          f"tape 1h net=${d1['net']/1e6:+.1f}M / 4h net=${d4['net']/1e6:+.1f}M")
    assert d1["net"] == 1_500_000 and d1["count"] == 2, "1h window must exclude the 3h-old clip"
    assert d4["net"] == 10_500_000 and d4["count"] == 3, "4h window must include all three"
    assert tape.ready(1800) is False, "tape not started -> never ready"

    # ---- v3.1: tiered tape — whale direction + whale-vs-retail divergence ----
    assert W.bias(0, 0) is None and W.bias(80, 20) == 0.6, "bias math"
    assert W.direction_read(0.3) == "ACCUMULATING HARD" and W.direction_read(-0.15) == "DISTRIBUTING" \
        and W.direction_read(0.0) == "BALANCED", "direction labels"
    assert W.divergence_read(0.3, -0.2)["key"] == "smart_accumulation"
    assert W.divergence_read(-0.3, 0.2)["key"] == "smart_distribution"
    assert W.divergence_read(0.2, 0.2)["key"] == "aligned_buying"
    assert W.divergence_read(0.0, 0.0)["key"] == "neutral"
    t2 = W.WhaleTapeMonitor(["BTCUSDT"], "", max_age_sec=14400,
                            min_clip_usd=100000, retail_max_usd=10000, bin_sec=900)
    now_ms = _time.time() * 1000
    t2.events.append((now_ms - 1000e3, "Buy", 1_000_000))    # ~17 min ago (bin -2)
    t2.events.append((now_ms - 300e3, "Buy", 500_000))       # 5 min ago (newest bin)
    t2.events.append((now_ms - 100e3, "Sell", 300_000))
    idx_now = int(now_ms / 1000) // 900
    t2.bins[idx_now] = [20_000.0, 80_000.0, 30_000.0, 10_000.0]   # retail selling hard
    sm = t2.summary(3600)
    print(f"tiered  whales {sm['whale']['direction']} bias={sm['whale']['bias']:+.2f} "
          f"vol=${sm['whale']['vol']/1e6:.1f}M share={sm['whale_share']:.2f} | "
          f"retail bias={sm['retail']['bias']:+.2f} -> {sm['divergence']['key']}")
    assert sm["whale"]["net"] == 1_200_000 and sm["whale"]["count"] == 3
    assert sm["whale"]["bias"] > 0.6 and sm["whale"]["direction"] == "ACCUMULATING HARD"
    assert sm["whale"]["max_clip"] == 1_000_000 and sm["whale"]["avg_clip"] == 600_000
    assert sm["retail"]["bias"] < 0, "retail tape must read net selling"
    assert sm["mid"]["net"] == 20_000, "mid tier sums independently"
    assert sm["divergence"]["key"] == "smart_accumulation", \
        "whales buying + retail selling = smart accumulation"
    assert sm["whale_share"] is not None and sm["whale_share"] > 0.9, "whales dominate this mock tape"
    nb = t2.whale_net_bins(4, 900)
    assert nb[-1] == 200_000 and nb[-2] == 1_000_000, "net bins map clips to the right time buckets"

    # ---- v4: per-symbol liquidation attribution ----
    import time as _t2
    import liquidations as LQ
    lm = LQ.LiquidationMonitor(["BTCUSDT", "SOLUSDT"], "", 5400, 900)
    _now = _t2.time() * 1000
    lm.events.append((_now - 60e3, "BTCUSDT", "Buy", 1_000_000))
    lm.events.append((_now - 50e3, "SOLUSDT", "Buy", 2_000_000))
    lm.events.append((_now - 40e3, "BTCUSDT", "Sell", 500_000))
    assert sum(lm.long_liq_bins(6, "BTCUSDT")) == 1_000_000, "BTC bins must exclude SOL's cascade"
    assert sum(lm.long_liq_bins(6)) == 3_000_000, "no symbol = pooled (the ladder's systemic read)"
    sol = lm.stats(900, "SOLUSDT")
    assert sol["long"] == 2_000_000 and sol["count"] == 1, "per-symbol stats must filter"
    print(f"liq-attribution  BTC-only=${sum(lm.long_liq_bins(6,'BTCUSDT'))/1e6:.0f}M  "
          f"pooled=${sum(lm.long_liq_bins(6))/1e6:.0f}M")

    # ---- v4: runtime watchlist (DB-backed, env is just the seed) ----
    ST.set_watchlist(["BTCUSDT", "DOGEUSDT"])
    assert ST.get_watchlist() == ["BTCUSDT", "DOGEUSDT"], "watchlist roundtrip"
    ST.add_cg_id("DOGEUSDT", "dogecoin")
    cg = ST.get_cg_map()
    assert cg["DOGEUSDT"] == "dogecoin" and cg["BTCUSDT"] == "bitcoin", \
        "runtime CG ids must merge over the hardcoded seeds"
    print(f"watchlist-store  {ST.get_watchlist()}  cg(DOGE)={cg['DOGEUSDT']}")

    # ---- v3.1: plain-English synthesis (the comms voice) ----
    import engine as E
    fake = {"ladder": {"raw_desire": 71.0, "macro_ceiling": 40, "confirm_cap": 30,
                       "confirm_count": 0, "target": 30.0},
            "fng": 9, "structural": 54.0, "regime": "HOSTILE", "overheat": 72.0,
            "sizing": {"action": "ADD", "actionable": True,
                       "current_pct": 0.0, "delta_pct": 30.0},
            "whales": {"summary": {"divergence": {
                "key": "smart_accumulation",
                "text": "whales are buying what retail is selling — classic bottom behavior"}}}}
    read = E.plain_read(fake)
    print(f"read  {read}")
    assert "deep fear" in read, "F&G 9 must read as deep fear"
    assert "Target 30%" in read and "0/3" in read, "binding gate must be the confirmation cap"
    assert "add about 30 points" in read, "actionable ADD must say so plainly"
    assert "overheat is flashing" in read, "overheat >= alert must warn"
    assert "whales are buying" in read, "smart-money divergence must surface"
    hold = E.plain_read({**fake, "overheat": 5.0, "whales": {},
                         "sizing": {"action": "HOLD", "actionable": False, "current_pct": 28.0}})
    assert "no action" in hold and "overheat" not in hold, "calm + in-position = quiet read"

    # ---- v3.2: plain_read — interpretation, not labels ----
    print("-" * 100)
    import engine
    base = {
        "ladder": {"raw_desire": 70.0, "macro_ceiling": 40, "confirm_cap": 50,
                   "confirm_count": 1, "target": 40},
        "fng": 9, "structural": 53.0, "funding": 0.00003,
        "regime": "HOSTILE", "reclaim_level": 75000,
        "confirms": {"btc_reclaim": False, "funding_positive": True, "fng_above_30": False},
        "trend": {"range_pos": 15.0, "px_vs_slow": -21.0, "rv_pctile": 49.0},
        "sizing": {"actionable": True, "action": "ADD", "delta_pct": 40.0, "current_pct": 0.0},
        "overheat": 2.0, "whales": {}, "degraded": [],
    }
    r1 = engine.plain_read(base)
    print("read(macro-bound):", r1)
    assert "scared in the head" in r1, "fear + positive funding must surface the head-vs-books tension"
    assert "floor of its 20-day range" in r1 and "below the 200-day" in r1, "location sentence missing"
    assert "the brake is your own HOSTILE macro setting" in r1 and "allow 50%" in r1, \
        "macro-bound read must say WHO holds the key and what the signals would allow"

    snap2 = dict(base)
    snap2["ladder"] = {"raw_desire": 70.0, "macro_ceiling": 70, "confirm_cap": 30,
                       "confirm_count": 0, "target": 30}
    snap2["fng"] = 13
    snap2["structural"] = 80.0
    snap2["funding"] = -0.0004
    r2 = engine.plain_read(snap2)
    print("read(confirm-bound):", r2)
    assert "genuinely washed out" in r2, "deep fear + high structure = washed-out wording"
    assert "hasn't proven the turn" in r2 and "next unlock is BTC back above $75,000" in r2, \
        "confirm-bound read must name the unlock level"

    # ---- v5: model scorecard (forward-return grading, pure) ----
    print("-" * 100)
    import scorecard as SC
    sc_rows, px = [], 100.0
    t0 = 1_000_000.0
    for i in range(100):                       # 50 "washed out" readings in a rally,
        struct_v = 80.0 if i < 50 else 20.0    # then 50 "not exhausted" in a slide
        sc_rows.append({"ts": t0 + i * 900, "btc_price": px, "structural": struct_v,
                        "fng": 50, "overheat": None, "whale_bias": None, "liq_flush": None})
        px += 0.5 if i < 50 else -0.5
    g = SC.grade(sc_rows, horizons={"4h": 14400}, min_n=10)
    h = g["horizons"]["4h"]
    assert 0 < h["matured"] < len(sc_rows), "immature tail must be excluded — no peeking"
    by_key = {b["key"]: b for b in h["buckets"]}
    hi, lo = by_key["struct_hi"], by_key["struct_lo"]
    assert hi["median_pct"] > 0 and hi["hit_rate"] >= 60 and hi["confident"], \
        "washed-out readings in the rally must grade bullish with confidence"
    assert lo["median_pct"] < 0, "not-exhausted readings in the slide must grade bearish"
    assert hi["edge_vs_base"] > 0 > lo["edge_vs_base"], "edges must straddle the baseline"
    print(f"scorecard  struct_hi: median {hi['median_pct']:+}% hit {hi['hit_rate']}% n={hi['n']} | "
          f"struct_lo: median {lo['median_pct']:+}% | matured {h['matured']}/{len(sc_rows)}")

    # ---- v5: adaptive overheat (percentile vs own history) ----
    assert baseline._pctile_high_is_hot(95, list(range(100))) > 90, "hot value ranks high"
    assert baseline._pctile_high_is_hot(2, list(range(100))) < 10, "calm value ranks low"
    val, is_adaptive = baseline.overheat_adaptive(42.0)   # test DB has no history yet
    assert val == 42.0 and is_adaptive is False, "no history -> static passthrough"
    print(f"overheat-adaptive  pctile(95)={baseline._pctile_high_is_hot(95, list(range(100)))} "
          f"passthrough={val}/{is_adaptive}")

    # ---- v5: divergence alert persistence (2 runs to fire, no re-fire) ----
    import engine as E2
    div_snap = {"ladder": {"target": 30}, "regime": "HOSTILE", "confirms": {}, "fng": 9,
                "whales": {"summary": {"divergence": {
                    "key": "smart_accumulation", "text": "whales are buying what retail is selling"}}}}
    base_prev = {"target": 30, "regime": "HOSTILE", "confirms": {}, "fng": 9}
    m1 = E2.diff_alerts({**base_prev, "whale_div_key": "neutral"}, dict(div_snap))
    assert not any("DIVERGENCE" in m for m in m1), "first appearance must NOT alert (needs 2 runs)"
    snap2 = dict(div_snap)
    m2 = E2.diff_alerts({**base_prev, "whale_div_key": "smart_accumulation"}, snap2)
    assert any("DIVERGENCE" in m for m in m2) and snap2.get("_div_alerted") == "smart_accumulation", \
        "second consecutive run must alert and mark itself"
    m3 = E2.diff_alerts({**base_prev, "whale_div_key": "smart_accumulation",
                         "whale_div_alerted": "smart_accumulation"}, dict(div_snap))
    assert not any("DIVERGENCE" in m for m in m3), "same verdict must not re-alert until it decays"
    print("divergence-persistence  run1=silent  run2=alert  run3=silent  ✓")

    # ---- v6: telegram bot (pure routing + formatting; no network) ----
    import telegram_bot as TB
    bot_snap = {
        "ladder": {"raw_desire": 42.3, "macro_ceiling": 40, "confirm_cap": 30,
                   "confirm_count": 0, "target": 30.0},
        "read": "The crowd is fearful; stay staged.",
        "sizing": {"action": "ADD", "actionable": True, "current_pct": 10.0,
                   "delta_pct": 20.0, "delta_usd": 2000.0},
        "fng": 22, "fng_class": "Extreme Fear", "structural": 54.0,
        "sentiment": 28.0, "funding": -0.000123, "oi_change": -0.032,
        "liq_flush": 12.0, "regime": "HOSTILE", "overheat": 18.0,
        "btc_price": 71234.0, "reclaim_level": 74000.0,
        "confirms": {"btc_reclaim": False, "funding_positive": False,
                     "fng_above_30": False},
        "degraded": [],
    }
    st = TB.fmt_status(bot_snap)
    assert "target 30%" in st and "held 10%" in st and "ADD $2,000" in st, \
        "status must carry target/held/tranche"
    why = TB.fmt_why(bot_snap)
    assert "confirmation cap" in why and "0/3" in why and "✗" in why, \
        "/why must name the binding gate and the unfired triggers"
    assert TB.binding_gate({"target": 40, "raw_desire": 70, "macro_ceiling": 40,
                            "confirm_cap": 50}).startswith("macro ceiling"), \
        "binding gate must identify the macro brake"
    ctx = {"snap": bot_snap, "scan": None,
           "watch": lambda s: f"added {s}", "unwatch": lambda s: f"removed {s}"}
    assert TB.route("/help", ctx) == TB.HELP
    assert TB.route("/status@SignaliaBot", ctx) == st, "@botname suffix must strip"
    assert TB.route("/watch SOL", ctx) == "added SOL"
    assert "usage" in TB.route("/watch", ctx), "missing arg -> usage hint"
    assert "unknown" in TB.route("/nope", ctx)
    assert "no scan yet" in TB.fmt_scan(None)
    r, cid = TB.handle_update({"message": {"chat": {"id": 999}, "text": "/status"}}, ctx)
    assert r is None and cid is None, "strangers must get silence"
    TB.C.TELEGRAM_CHAT_ID = "999"
    r, cid = TB.handle_update({"message": {"chat": {"id": 999}, "text": "/status"}}, ctx)
    assert cid == "999" and "target 30%" in r, "owner chat must get the status"
    print("telegram-bot  /status·/why·routing·auth ✓  binding-gate=confirmation-cap")

    # ---- v6: backup config gating (no network — just the guard rails) ----
    assert ST.restore_db().get("reason") == "backup not configured", \
        "unconfigured restore must no-op"
    assert ST.backup_db().get("reason") == "backup not configured", \
        "unconfigured backup must no-op"
    print("db-backup  unconfigured -> clean no-op ✓")

    print("\nAll sanity checks passed.")
    print("Reads correctly: the binding gate shifts from confirmation-cap (deep fear, "
          "no price confirm) to macro-ceiling (once price confirms) — exactly the staging discipline.")
