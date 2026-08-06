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
- Blends completion quality (45%), full completion ratio (25%), consistency rhythm
  (15%), and recent momentum (15%):
  `Score = 0.45Q + 0.25F + 0.15R + 0.15M`.
- Completion quality is `Q = sum(clamped progress) / scheduled sessions`.
  Missing scheduled sessions contribute 0%, and partial progress contributes
  proportionally.
- Full completion is
  `F = 100 × sessions at exactly 100% / scheduled sessions`. A 99% session
  contributes to Q but not F.
- Consistency rhythm uses the last seven scheduled sessions in the selected
  report period: 80% comes from success coverage above **50%** and 20% from
  consecutive successful sessions. With `k` observations, confidence is
  `min(1, k / 3)`; one or two observations are shrunk toward a neutral 50% until
  three observations exist. More precisely, `R = 100 × (0.5 + c × (rawR - 0.5))`,
  where `rawR = 0.8 × coverage + 0.2 × continuity` and `c = min(1, k / 3)`.
  A history with no successful sessions remains 0%.
- Recent momentum uses up to six scheduled sessions and five changes. Changes
  within ±5 points count as stable; larger changes are recency-weighted,
  cadence-neutral, and confidence-adjusted with the same `min(1, k / 3)` rule.
  It is also scaled by evidence `min(1, latest progress / 50)`, so stable 5%
  and 10% histories score only 5% and 10% Momentum instead of receiving 50%.
  Each change signal is 0 inside the ±5-point band; outside it, the signal is
  `sign(change) × min(1, (abs(change) - 5) / 45)`. Newer transitions receive
  ordinal weights `1, 2, ...`.
  More precisely, `M = 100 × evidence × (0.5 + 0.5 × c × trend)`, clamped to
  0–100, where `trend` is that recency-weighted average.
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
