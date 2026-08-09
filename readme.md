# Consistify - A Habit Tracker

A modern, analytics-driven Habit Tracking web application built with Django. It helps users build consistency, track daily habits, and analyze their performance through smart metrics, streaks, and visual insights.

<br>

## 🚀 Project Overview

This Habit Tracker allows users to create and manage multiple habits with flexible tracking methods.  
Each habit is tracked using a **unified percentage-based system**, enabling accurate analytics and meaningful performance insights.

The system focuses on:
- Consistency building
- Habit performance analysis
- Visual progress tracking
- Discipline measurement


<br>

## ✨ Key Features

### 🔐 Authentication
- User registration and login system
- Secure user-specific habit management


<br>

### 🧩 Habit Management
Users can create multiple habits with different tracking types:

#### 1. Binary Habit
- Simple Done / Not Done tracking
- Stored as:
  - Done → 100%
  - Not Done → 0%

#### 2. Partial Habit
- Slider-based tracking (0–100%)
- Optional numeric input support

#### 3. Quantitative Habit
- Track measurable goals (e.g., water intake, study hours)
- Based on:
  - Current value / Target value → percentage completion


<br>

### 📊 Unified Tracking System
- All habits are tracked using a **single field: `completion_percentage`**
- All performance metrics are derived from this unified model


<br>

## 📈 Analytics & Insights

#### 📊 Average Scheduled-Session Progress
- Every completion rate uses one shared priority-weighted average across
  **every scheduled session**
- Each session contributes its partial completion percentage, weighted by
  High `1.3`, Medium `1.0`, or Low `0.8`
- Missing scheduled sessions count as 0%, including sessions due today
- Schedule, priority, and category settings are effective-dated. Plan edits
  begin the next day, so historical due dates and report weights cannot be
  rewritten by a later configuration change
- The first plan-version migration snapshots each habit's then-current plan;
  configuration changes made before that migration cannot be reconstructed
- Used for:
  - Habit progress evaluation
  - Performance reports
  - Global statistics

#### 🧠 Consistency Score
- Blends completion quality (35%), full completion (20%), consistency rhythm
  (30%), and recent momentum (15%):
  `Score = 0.35Q + 0.20F + E × (0.30R + 0.15M)`, where `E` is the evidence
  factor described below.
- **Completion quality (Q)** measures how much of the scheduled work was
  actually completed across every scheduled session, including partial
  progress. `Q = sum(clamped progress) / scheduled sessions`. Missing
  scheduled sessions contribute 0%, and partial progress contributes
  proportionally. Q is the longest-window signal and anchors the overall
  completion level.
- **Full completion (F)** is a finishing-quality signal:
  `F = 100 × sessions at exactly 100% / scheduled sessions`. A 99% session
  contributes to Q but not F. F exists to prevent partial-credit gaming
  without duplicating Q.
- **Consistency rhythm (R)** measures reliability and continuity — how
  consistently the user keeps showing up for recent scheduled sessions and
  avoids interruptions. It is a *level* signal, not a *trend* signal;
  improvement and decline are reported separately by Recent Momentum. R uses
  the last seven scheduled sessions in the selected report period: 80% comes
  from success coverage above **50%** and 20% from consecutive successful
  sessions. With `k` observations, confidence is `min(1, k / 3)`; one or two
  observations are shrunk toward a neutral 50% until three observations exist.
  More precisely, `R = 100 × (0.5 + c × (rawR - 0.5))`, where
  `rawR = 0.8 × coverage + 0.2 × continuity` and `c = min(1, k / 3)`. With
  one observation there are no transitions, so `continuity = coverage` and
  `rawR = coverage`. An all-zero-success history produces 0% R at full
  confidence.
- **Recent momentum (M)** measures trajectory — whether recent performance
  is improving, declining, or remaining stable compared with the user's
  recent baseline. It is a *trend* signal, not a *reliability* signal;
  regularity and missed-session avoidance are reported separately by
  Consistency Rhythm. M is centred at 50 (neutral), so a flat user does not
  gain or lose ground from momentum alone. It uses up to six scheduled
  sessions and five changes. Changes within ±5 points count as stable; larger
  changes are recency-weighted, cadence-neutral, and confidence-adjusted with
  the same `min(1, k / 3)` rule. It is also scaled by evidence
  `min(1, latest progress / 50)`, so stable 5% and 10% histories score only
  5% and 10% Momentum instead of receiving 50%. Each change signal is 0
  inside the ±5-point band; outside it, the signal is
  `sign(change) × min(1, (abs(change) - 5) / 45)`. Newer transitions receive
  ordinal weights `1, 2, ...`. More precisely,
  `M = 100 × evidence × (0.5 + 0.5 × c × trend)`, clamped to 0–100, where
  `trend` is that recency-weighted average.
- **Evidence factor (E)** decides how much of the Rhythm and Momentum *points*
  reach the score. Completion can be earned immediately, but consistency has to
  be demonstrated through repeated behaviour, so a habit with a single
  scheduled session has not yet shown any cadence, continuity, or trajectory.
  With `k` scheduled sessions in the report period,
  `E = 1 - exp(-(k / 1.3) ** 0.75)`. The exponent below 1 front-loads the curve
  so the first repeats are worth the most evidence and each later one a little
  less — the same intuition as the `min(1, k / 3)` confidence inside R and M,
  but smooth instead of kinked. E only scales the contribution: the reported R
  and M values are exactly as measured, `E` can never add points (so an
  untouched record still scores 0), and it saturates at 1 — roughly 0.56 at one
  session, 0.75 at two, 0.85 at three, 0.94 at five, 0.99 at ten, and
  effectively 1.0 for any longer history, so established habits keep the
  long-term scoring behaviour. A perfectly kept habit therefore scores about
  70 at 1/1, 79 at 2/2, 87 at 3/3, 90 at 5/5, and 92-93 from 10/10 onward.
- Rhythm and Momentum use only scheduled sessions inside the selected report
  period; history outside that period cannot change its score.
- Cross-habit scores use a weighted mean. Habit `h` receives aggregation weight
  `average active priority weight × sqrt(scheduled sessions)`; priority is
  resolved from the plan active on each occurrence.

#### 🔥 Streak System
- Tracks consecutive fully completed days (100% only)
- Includes:
  - Current streak
  - Longest streak

#### 📉 Habit Performance
- Best day tracking
- Completion trends over time
- Missed vs completed analysis


<br>

## 📱 UI/UX Features
- Fully responsive design (mobile-friendly)
- Dark mode support 🌙
- Clean dashboard-style interface
- Drag & drop habit ordering
- Progress visualization using charts and graphs


<br>

## 📊 Habit Detail Page
Each habit includes:
- Completion history
- Current & longest streak
- Average daily value
- Consistency score
- Performance charts (trend visualization)


<br>

## 🌍 Global Dashboard
- Overview of all habits
- Highlights:
  - Best performing habits
  - Weak performing habits
  - Overall discipline summary
- Aggregated consistency insights across all habits
