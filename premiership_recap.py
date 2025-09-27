#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Premiership Weekly Fantasy Newsletter Generator for Sleeper
(Ron Burgundy energy, banter at TEAM level only, no personal jabs)

- Pulls league, users, rosters, and all completed weeks' matchups.
- Produces: <LEAGUE_NAME>_Weeks<first>_<last>_Recap.pdf
- Sections:
  1) Standings table (Team, W, L, PF, PA, Diff) sorted by Wins desc, then PF desc.
  2) Strength of Schedule & Luck table with:
       Team, W, L, PF, PA, SOS (OppAvg), All-Play%, Exp W, Luck
     * All-Play% = weekly all-play win rate averaged across weeks.
     * Exp W = All-Play% × Games Played.
     * Luck = W − Exp W.
     * Luck badges rendered with ReportLab Paragraph and <font color='...'>:
         🍀 green (> +0.5), 😬 red (< −0.5), ⚖️ gray otherwise.
     * Includes a “League Avg” row for SOS at the bottom (kept LAST).
     * Sort table by Wins desc, then PF desc, with “League Avg” last.
  3) Highlights:
     - Heart-Attack Matchups (closest margin each week)
     - Blowouts of the Week (largest margin each week)
  4) Banter sections (five rotating lines per category, random.choice):
     - Top Dogs (undefeated)
     - Doormats (winless)
     - Luckiest (top +Luck)
     - Unluckiest (most −Luck)
     - Easiest Schedule (lowest SOS)
     - Hardest Schedule (highest SOS)
  5) League Story So Far — one paragraph: top/bottom team, PF leader/laggard,
     easiest/hardest schedule, luckiest/unluckiest, closest & biggest blowout overall.

- Config: environment variable SLEEPER_LEAGUE_ID (defaults to 1180243437903376384)
- Dependencies: requests, reportlab
"""

import os
import math
import random
import datetime
from collections import defaultdict

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.units import inch

BASE = "https://api.sleeper.app/v1"

# -----------------------------
# Helpers: Sleeper API
# -----------------------------
def get_json(url):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()

def get_league(league_id):
    return get_json(f"{BASE}/league/{league_id}")

def get_users(league_id):
    return get_json(f"{BASE}/league/{league_id}/users")

def get_rosters(league_id):
    return get_json(f"{BASE}/league/{league_id}/rosters")

def get_matchups(league_id, week):
    return get_json(f"{BASE}/league/{league_id}/matchups/{week}")

# -----------------------------
# Core calculations
# -----------------------------
def is_week_completed(matchups_for_week):
    """A week is considered 'completed' if:
    - there is at least one matchup_id,
    - every matchup pair has two teams,
    - and at least one team scored > 0 (avoid pre-lock 0-0s).
    """
    if not matchups_for_week:
        return False
    by_id = defaultdict(list)
    nonzero_points_present = False
    for m in matchups_for_week:
        if "matchup_id" not in m:
            return False
        by_id[m["matchup_id"]].append(m)
        try:
            pts = float(m.get("points", 0) or 0.0)
            if pts > 0:
                nonzero_points_present = True
        except Exception:
            pass
    if not nonzero_points_present:
        return False
    for mid, entries in by_id.items():
        if len(entries) < 2:
            return False
    return True

def build_id_maps(users, rosters):
    """Returns mapping dicts:
    - roster_id -> team display name
    - owner_id -> owner display name
    - roster_id -> owner_id
    """
    user_map = {u["user_id"]: u for u in users}
    owner_display = {}
    for u in users:
        dn = (u.get("metadata", {}) or {}).get("team_name") or u.get("display_name") or "Unknown"
        owner_display[u["user_id"]] = dn
    roster_to_owner = {}
    roster_to_teamname = {}
    for r in rosters:
        rid = r["roster_id"]
        owner_id = r.get("owner_id")
        roster_to_owner[rid] = owner_id
        team_name = (r.get("metadata") or {}).get("team_name") \
                    or (user_map.get(owner_id, {}).get("metadata") or {}).get("team_name") \
                    or owner_display.get(owner_id, "Unknown")
        roster_to_teamname[rid] = team_name
    return roster_to_teamname, owner_display, roster_to_owner

def pair_matchups(matchups_for_week):
    """Return list of (rid_a, pts_a, rid_b, pts_b) pairs for the week."""
    by_id = defaultdict(list)
    for m in matchups_for_week:
        by_id[m["matchup_id"]].append(m)
    pairs = []
    for mid, entries in by_id.items():
        if len(entries) >= 2:
            e = sorted(entries, key=lambda x: x.get("roster_id"))
            a, b = e[0], e[1]
            ra = a.get("roster_id")
            rb = b.get("roster_id")
            pa = float(a.get("points", 0) or 0.0)
            pb = float(b.get("points", 0) or 0.0)
            pairs.append((ra, pa, rb, pb))
    return pairs

def collect_weeks_completed(league_id, max_weeks=30):
    """Scan weeks, keep only completed ones."""
    completed = []
    for w in range(1, max_weeks + 1):
        try:
            mu = get_matchups(league_id, w)
        except requests.HTTPError:
            break
        if not mu:
            continue
        if is_week_completed(mu):
            completed.append((w, pair_matchups(mu)))
    return completed

def compute_team_level_stats(roster_to_name, weeks_pairs):
    """
    Returns:
    - standings: dict[roster_id] = {team, W, L, PF, PA, Diff, GP}
    - weekly_points: dict[week] = dict[roster_id] = points (for All-Play)
    - schedule: dict[roster_id] = list of opponent roster_ids (for SOS)
    """
    standings = {}
    weekly_points = defaultdict(dict)
    schedule = defaultdict(list)

    # init
    for rid in roster_to_name:
        standings[rid] = dict(team=roster_to_name[rid], W=0.0, L=0.0, PF=0.0, PA=0.0, Diff=0.0, GP=0)

    for week, pairs in weeks_pairs:
        # capture weekly PF for all-play
        for ra, pa, rb, pb in pairs:
            weekly_points[week][ra] = pa
            weekly_points[week][rb] = pb

        for ra, pa, rb, pb in pairs:
            # schedule
            schedule[ra].append(rb)
            schedule[rb].append(ra)

            # PF/PA
            standings[ra]["PF"] += pa
            standings[ra]["PA"] += pb
            standings[rb]["PF"] += pb
            standings[rb]["PA"] += pa

            standings[ra]["GP"] += 1
            standings[rb]["GP"] += 1

            # W/L
            if pa > pb:
                standings[ra]["W"] += 1
                standings[rb]["L"] += 1
            elif pb > pa:
                standings[rb]["W"] += 1
                standings[ra]["L"] += 1
            else:
                # ties rare; split the point
                standings[ra]["W"] += 0.5
                standings[rb]["W"] += 0.5
                standings[ra]["L"] += 0.5
                standings[rb]["L"] += 0.5

    for rid in standings:
        standings[rid]["Diff"] = standings[rid]["PF"] - standings[rid]["PA"]

    return standings, weekly_points, schedule

def compute_all_play(weekly_points):
    """
    All-Play% per team: for each week, win rate vs every other team's weekly score.
    Then average across weeks played by that team.
    Returns dict[roster_id] = all_play_rate (0..1)
    """
    per_team_week_rates = defaultdict(list)
    for week, pts_map in weekly_points.items():
        items = list(pts_map.items())
        for rid, pts in items:
            wins = 0.0
            total = 0.0
            for orid, opts in items:
                if orid == rid:
                    continue
                total += 1
                if pts > opts:
                    wins += 1
                elif pts == opts:
                    wins += 0.5
            if total > 0:
                per_team_week_rates[rid].append(wins / total)

    all_play = {}
    for rid, rates in per_team_week_rates.items():
        all_play[rid] = sum(rates) / len(rates) if rates else 0.0
    return all_play

def compute_sos(schedule, standings):
    """
    SOS (OppAvg): average of opponents' PF per game (season-to-date) for all games faced.
    Returns dict[roster_id] = sos_value
    """
    pf_per_game = {}
    for rid, st in standings.items():
        gp = st["GP"] if st["GP"] > 0 else 1
        pf_per_game[rid] = st["PF"] / gp

    sos = {}
    for rid, opp_list in schedule.items():
        if opp_list:
            sos[rid] = sum(pf_per_game[o] for o in opp_list) / len(opp_list)
        else:
            sos[rid] = 0.0
    return sos

def fmt_num(x, nd=2):
    return f"{x:.{nd}f}"

def luck_badge(luck_val):
    """Return a styled Paragraph with colored badge."""
    styles = getSampleStyleSheet()
    if luck_val > 0.5:
        col = "#0a8a0a"
        icon = "🍀"
    elif luck_val < -0.5:
        col = "#c1121f"
        icon = "😬"
    else:
        col = "#6b7280"
        icon = "⚖️"
    return Paragraph(f"<font color='{col}'>{icon} {fmt_num(luck_val,2)}</font>", styles["BodyText"])

# -----------------------------
# Report assembly (PDF)
# -----------------------------
def sorted_rows(standings_rows):
    """Sort by Wins desc, then PF desc."""
    return sorted(standings_rows, key=lambda r: (r["W"], r["PF"]), reverse=True)

def make_standings_table(standings):
    rows = []
    for rid, st in standings.items():
        rows.append(dict(
            Team=st["team"],
            W=st["W"],
            L=st["L"],
            PF=st["PF"],
            PA=st["PA"],
            Diff=st["Diff"]
        ))
    rows = sorted_rows(rows)
    data = [["Team", "W", "L", "PF", "PA", "Diff"]]
    for r in rows:
        data.append([
            r["Team"],
            f"{r['W']:.0f}" if float(r["W"]).is_integer() else fmt_num(r["W"],1),
            f"{r['L']:.0f}" if float(r["L"]).is_integer() else fmt_num(r["L"],1),
            fmt_num(r["PF"]),
            fmt_num(r["PA"]),
            fmt_num(r["Diff"])
        ])
    tbl = Table(data, hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.Color(0.96,0.96,0.96)])
    ]))
    return tbl

def make_sos_luck_table(standings, all_play, sos):
    rows = []
    for rid, st in standings.items():
        gp = st["GP"]
        ap = all_play.get(rid, 0.0)
        exp_w = ap * gp
        luck = st["W"] - exp_w
        rows.append(dict(
            Team=st["team"],
            W=st["W"],
            L=st["L"],
            PF=st["PF"],
            PA=st["PA"],
            SOS=sos.get(rid, 0.0),
            AllPlay=ap,
            ExpW=exp_w,
            Luck=luck
        ))

    league_avg_sos = sum(r["SOS"] for r in rows) / len(rows) if rows else 0.0

    # Sort by Wins desc, then PF desc (League Avg not included in sort)
    rows_sorted = sorted(rows, key=lambda r: (r["W"], r["PF"]), reverse=True)

    data = [["Team", "W", "L", "PF", "PA", "SOS (OppAvg)", "All-Play%", "Exp W", "Luck"]]
    for r in rows_sorted:
        data.append([
            r["Team"],
            f"{r['W']:.0f}" if float(r["W"]).is_integer() else fmt_num(r["W"],1),
            f"{r['L']:.0f}" if float(r["L"]).is_integer() else fmt_num(r["L"],1),
            fmt_num(r["PF"]),
            fmt_num(r["PA"]),
            fmt_num(r["SOS"]),
            fmt_num(r["AllPlay"]*100,1)+"%",
            fmt_num(r["ExpW"],2),
            luck_badge(r["Luck"])
        ])
    # League Avg row LAST
    data.append([
        "League Avg",
        "-", "-", "-", "-",
        fmt_num(league_avg_sos),
        "-", "-", Paragraph("<font color='#6b7280'>—</font>", getSampleStyleSheet()["BodyText"])
    ])

    tbl = Table(data, hAlign="LEFT", repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN", (1,1), (-2,-2), "RIGHT"),
        ("VALIGN", (-1,1), (-1,-2), "MIDDLE"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.whitesmoke, colors.Color(0.96,0.96,0.96)]),
        ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#e5e7eb")),
    ]))
    return tbl, rows_sorted, league_avg_sos

def week_highlights(weeks_pairs, roster_to_name):
    """Return dicts for closest and biggest margin per week, plus overall closest/biggest."""
    heart = {}   # week -> (teamA, ptsA, teamB, ptsB, margin)
    blow = {}    # week -> (...)
    overall_closest = None
    overall_blowout = None

    for week, pairs in weeks_pairs:
        closest = None
        biggest = None
        for ra, pa, rb, pb in pairs:
            margin = abs(pa - pb)
            pair = (roster_to_name[ra], pa, roster_to_name[rb], pb, margin)
            if closest is None or margin < closest[-1]:
                closest = pair
            if biggest is None or margin > biggest[-1]:
                biggest = pair
        if closest: heart[week] = closest
        if biggest: blow[week] = biggest

        if closest and (overall_closest is None or closest[-1] < overall_closest[-1]):
            overall_closest = (week,) + closest
        if biggest and (overall_blowout is None or biggest[-1] > overall_blowout[-1]):
            overall_blowout = (week,) + biggest

    return heart, blow, overall_closest, overall_blowout

def pick_category_line(category, teams):
    """Return one of five rotating lines for the category with Ron Burgundy-esque flair."""
    t = ", ".join(teams) if isinstance(teams, (list, tuple)) else str(teams)
    bank = {
        "Top Dogs": [
            f"{t} remain classier than a mahogany scotch cabinet—still undefeated.",
            f"Stay classy: {t} haven’t tasted defeat yet.",
            f"{t} are on a heater so hot it needs an SPF rating.",
            f"Undefeated and unbothered: {t} keep jazz-fluting to victory.",
            f"{t} are so dominant the league asked for a wellness check."
        ],
        "Doormats": [
            f"{t} keep holding the door like courteous bellhops—wins not included.",
            f"It’s brisk out; {t} brought the L-sweaters again.",
            f"{t} are allergic to the letter W—someone call the pharmacist.",
            f"The rebuild is on schedule… if the schedule is 2087. Chin up, {t}.",
            f"{t} promise they’re fine. They said that through a smile."
        ],
        "Luckiest": [
            f"{t} have more horseshoes than a Kentucky derby—variance loves you.",
            f"Lady Luck keeps texting {t} back. Respect.",
            f"The fantasy gods winked at {t}; results ensued.",
            f"{t} caught all the bounces like they’re spring-loaded.",
            f"{t} are living proof that fortune favors the fabulous."
        ],
        "Unluckiest": [
            f"{t} stepped on every rake in the lawn—hang in there.",
            f"The stat gremlins keep nibbling at {t}’s ankles.",
            f"{t} have earned a ceremonial re-roll from the universe.",
            f"If pain built character, {t} would be prestige TV.",
            f"{t} keep drawing short straws in an industrial straw factory."
        ],
        "Easiest": [
            f"{t} found the travelator—schedule slants downhill nicely.",
            f"The path is paved in velvet for {t}. Enjoy the glide.",
            f"{t} are sipping umbrella drinks on Schedule Beach.",
            f"Matchups part like curtains for {t}. Encore!",
            f"{t} booked the deluxe itinerary—minimal turbulence."
        ],
        "Hardest": [
            f"{t} took the scenic route through Mordor—respect the grind.",
            f"Every week is leg day for {t}. Quads of steel.",
            f"{t} keep drawing boss fights on Nightmare difficulty.",
            f"The gauntlet respects {t}, even if the standings don’t.",
            f"{t} brought a jazz flute to a knife fight and still swingin’."
        ]
    }
    return random.choice(bank[category])

def build_story_paragraph(styles, standings_sorted, sos_rows_sorted, luck_sorted, overall_close, overall_blow):
    top_team = standings_sorted[0]["Team"] if standings_sorted else "—"
    bottom_team = standings_sorted[-1]["Team"] if standings_sorted else "—"

    pf_leader = max(standings_sorted, key=lambda r: r["PF"])["Team"] if standings_sorted else "—"
    pf_laggard = min(standings_sorted, key=lambda r: r["PF"])["Team"] if standings_sorted else "—"

    easiest = sos_rows_sorted[0]["Team"] if sos_rows_sorted else "—"
    hardest = sos_rows_sorted[-1]["Team"] if sos_rows_sorted else "—"

    luckiest = luck_sorted[0]["Team"] if luck_sorted else "—"
    unluckiest = luck_sorted[-1]["Team"] if luck_sorted else "—"

    if overall_close:
        w, a, pa, b, pb, m = overall_close
        close_txt = f"closest finish came in Week {w}: {a} {fmt_num(pa,1)} vs {b} {fmt_num(pb,1)} (margin {fmt_num(m,1)})."
    else:
        close_txt = "no nail-biters recorded yet."

    if overall_blow:
        w, a, pa, b, pb, m = overall_blow
        blow_txt = f"largest blowout was Week {w}: {a} {fmt_num(pa,1)} vs {b} {fmt_num(pb,1)} (margin {fmt_num(m,1)})."
    else:
        blow_txt = "no blowouts on record yet."

    txt = (
        f"In a season spicier than aftershave in the eyes, {top_team} currently rule the roost while "
        f"{bottom_team} are searching for their rhythm. Points cannons? That’d be {pf_leader}. "
        f"Meanwhile, {pf_laggard} are saving their fireworks for later. The schedule gods smiled on {easiest} "
        f"and tossed anvils at {hardest}. Variance has kissed {luckiest} on both cheeks, while {unluckiest} "
        f"could use a cosmic refund. For the drama lovers, the {close_txt} And for the chaos enthusiasts, the {blow_txt} Stay classy."
    )
    return Paragraph(txt, styles["BodyText"])

# -----------------------------
# Main
# -----------------------------
def main():
    league_id = os.environ.get("SLEEPER_LEAGUE_ID", "1180243437903376384")

    league = get_league(league_id)
    league_name = league.get("name", f"League_{league_id}")
    users = get_users(league_id)
    rosters = get_rosters(league_id)
    roster_to_name, _, _ = build_id_maps(users, rosters)

    completed = collect_weeks_completed(league_id)
    if not completed:
        first_week = last_week = "NA"
    else:
        first_week = completed[0][0]
        last_week = completed[-1][0]

    standings, weekly_points, schedule = compute_team_level_stats(roster_to_name, completed)
    all_play = compute_all_play(weekly_points)
    sos = compute_sos(schedule, standings)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Hed", fontName="Helvetica-Bold", fontSize=16, spaceAfter=8))
    styles.add(ParagraphStyle(name="SubHed", fontName="Helvetica-Bold", fontSize=12, spaceAfter=6))
    styles.add(ParagraphStyle(name="TinyGray", fontSize=8, textColor=colors.grey))

    doc_title = f"{league_name} — Weeks {first_week}–{last_week} Recap"
    out_name = f"{league_name}_Weeks{first_week}_{last_week}_Recap.pdf"

    story = []
    story.append(Paragraph(doc_title, styles["Hed"]))
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    story.append(Paragraph(f"Generated {today}", styles["TinyGray"]))
    story.append(Spacer(1, 0.2*inch))

    # Standings
    story.append(Paragraph("Standings", styles["SubHed"]))
    standings_tbl = make_standings_table(standings)
    story.append(standings_tbl)
    story.append(Spacer(1, 0.2*inch))

    # SOS & Luck
    story.append(Paragraph("Strength of Schedule & Luck", styles["SubHed"]))
    sos_tbl, sos_rows_sorted, league_avg_sos = make_sos_luck_table(standings, all_play, sos)
    story.append(sos_tbl)
    story.append(Spacer(1, 0.2*inch))

    # Highlights
    story.append(Paragraph("Highlights", styles["SubHed"]))
    heart, blow, overall_close, overall_blow = week_highlights(completed, roster_to_name)

    # Heart-Attack Matchups
    if heart:
        lines = []
        for wk in sorted(heart.keys()):
            a, pa, b, pb, m = heart[wk]
            lines.append(f"Week {wk}: {a} {fmt_num(pa,1)} vs {b} {fmt_num(pb,1)} — margin {fmt_num(m,1)}")
        story.append(Paragraph("<b>Heart-Attack Matchups</b><br/>" + "<br/>".join(lines), styles["BodyText"]))
    else:
        story.append(Paragraph("<b>Heart-Attack Matchups</b><br/>No nail-biters yet.", styles["BodyText"]))
    story.append(Spacer(1, 0.1*inch))

    # Blowouts
    if blow:
        lines = []
        for wk in sorted(blow.keys()):
            a, pa, b, pb, m = blow[wk]
            lines.append(f"Week {wk}: {a} {fmt_num(pa,1)} vs {b} {fmt_num(pb,1)} — margin {fmt_num(m,1)}")
        story.append(Paragraph("<b>Blowouts of the Week</b><br/>" + "<br/>".join(lines), styles["BodyText"]))
    else:
        story.append(Paragraph("<b>Blowouts of the Week</b><br/>No blowouts recorded.", styles["BodyText"]))
    story.append(Spacer(1, 0.2*inch))

    # Banter sections (team-level, rotating lines)
    standings_rows = []
    for rid, st in standings.items():
        standings_rows.append({
            "Team": st["team"],
            "W": st["W"],
            "PF": st["PF"],
            "GP": st["GP"],
            "RID": rid
        })
    standings_sorted = sorted(standings_rows, key=lambda r: (r["W"], r["PF"]), reverse=True)

    undefeated = [r["Team"] for r in standings_rows if r["GP"] > 0 and math.isclose(r["W"], r["GP"], rel_tol=1e-9)]
    winless = [r["Team"] for r in standings_rows if r["GP"] > 0 and math.isclose(r["W"], 0.0, rel_tol=1e-9)]

    # Luck extremes
    luck_list = []
    for r in standings_rows:
        ap = all_play.get(r["RID"], 0.0)
        expw = ap * r["GP"]
        luck_val = r["W"] - expw
        luck_list.append({"Team": r["Team"], "Luck": luck_val})
    luckiest_sorted = sorted(luck_list, key=lambda x: x["Luck"], reverse=True)
    unluckiest_sorted = sorted(luck_list, key=lambda x: x["Luck"])
    luckiest_teams = [luckiest_sorted[0]["Team"]] if luckiest_sorted else []
    unluckiest_teams = [unluckiest_sorted[0]["Team"]] if unluckiest_sorted else []

    # SOS extremes
    sos_rows = [{"Team": standings[rid]["team"], "SOS": sos.get(rid, 0.0)} for rid in standings.keys()]
    sos_rows_sorted_only = sorted(sos_rows, key=lambda x: x["SOS"])
    easiest_teams = [sos_rows_sorted_only[0]["Team"]] if sos_rows_sorted_only else []
    hardest_teams = [sos_rows_sorted_only[-1]["Team"]] if sos_rows_sorted_only else []

    # Render banter
    banter_sections = []
    banter_sections.append(("<b>Top Dogs</b>", pick_category_line("Top Dogs", undefeated or ["(none)"])))
    banter_sections.append(("<b>Doormats</b>", pick_category_line("Doormats", winless or ["(none)"])))
    if luckiest_teams:
        banter_sections.append(("<b>Luckiest</b>", pick_category_line("Luckiest", luckiest_teams)))
    if unluckiest_teams:
        banter_sections.append(("<b>Unluckiest</b>", pick_category_line("Unluckiest", unluckiest_teams)))
    if easiest_teams:
        banter_sections.append(("<b>Easiest Schedule</b>", pick_category_line("Easiest", easiest_teams)))
    if hardest_teams:
        banter_sections.append(("<b>Hardest Schedule</b>", pick_category_line("Hardest", hardest_teams)))

    for hed, line in banter_sections:
        story.append(Paragraph(f"{hed}<br/>{line}", styles["BodyText"]))
        story.append(Spacer(1, 0.08*inch))

    story.append(Spacer(1, 0.15*inch))

    # League Story So Far
    sos_rows_sorted_full = sorted(
        [{"Team": standings[r]["team"], "W": standings[r]["W"], "PF": standings[r]["PF"], "SOS": sos.get(r,0.0)}
         for r in standings],
        key=lambda r: r["SOS"]
    )
    luck_sorted_full = sorted(
        [{"Team": standings[r]["team"], "Luck": (standings[r]["W"] - all_play.get(r,0.0)*standings[r]["GP"])}
         for r in standings],
        key=lambda r: r["Luck"],
        reverse=True
    )
    story.append(Paragraph("League Story So Far", styles["SubHed"]))
    story.append(build_story_paragraph(styles, standings_sorted, sos_rows_sorted_full, luck_sorted_full, overall_close, overall_blow))

    # Build PDF
    doc = SimpleDocTemplate(out_name, pagesize=LETTER, leftMargin=36, rightMargin=36, topMargin=40, bottomMargin=40)
    doc.build(story)
    print(f"Generated: {out_name}")

if __name__ == "__main__":
    main()
