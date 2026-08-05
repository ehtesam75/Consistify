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
- Historical completion is the average percentage across **every scheduled session**
- Missing scheduled sessions count as 0%, including sessions due today
- The Today summary is priority-adjusted using Low `0.8`, Medium `1.0`, and High `1.3`
- Used for:
  - Habit progress evaluation
  - Performance reports
  - Global statistics

#### 🧠 Consistency Score
- Blends completion quality (45%), full completion ratio (25%), rhythm stability
  (15%), and recent momentum (15%)
- Partial progress contributes to completion quality and momentum, while a session
  counts as fully completed only at **100%**
- Recent momentum uses a 14-day window anchored to the latest eligible scheduled
  session, weighting newer sessions more heavily without penalizing unscheduled or
  paused dates

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
