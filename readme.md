# 🧠 Consistify - A Habit Tracker

A modern, analytics-driven Habit Tracking web application built with Django.  
It helps users build consistency, track daily habits, and analyze their performance through smart metrics, streaks, and visual insights.

---

## 🚀 Project Overview

This Habit Tracker allows users to create and manage multiple habits with flexible tracking methods.  
Each habit is tracked using a **unified percentage-based system**, enabling accurate analytics and meaningful performance insights.

The system focuses on:
- Consistency building
- Habit performance analysis
- Visual progress tracking
- Discipline measurement

---

## ✨ Key Features

### 🔐 Authentication
- User registration and login system
- Secure user-specific habit management

---

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

---

### 📊 Unified Tracking System
- All habits are tracked using a **single field: `completion_percentage`**
- All performance metrics are derived from this unified model

---

### 📈 Analytics & Insights

#### 📊 Average Daily Value
- Overall completion is calculated using **average daily performance**
- Used for:
  - Habit progress evaluation
  - Performance reports
  - Global statistics

#### 🧠 Consistency Score
- Measures discipline based on full completion behavior
- Only days with **100% completion** are counted

#### 🔥 Streak System
- Tracks consecutive fully completed days (100% only)
- Includes:
  - Current streak
  - Longest streak

#### 📉 Habit Performance
- Best day tracking
- Completion trends over time
- Missed vs completed analysis

---

### 🗂 Organization System
- Categories:
  - Health
  - Study
  - Work
  - Personal

- Tags support:
  - #morning
  - #focus
  - #fitness

- Priority levels:
  - High
  - Medium
  - Low

---

### 📱 UI/UX Features
- Fully responsive design (mobile-friendly)
- Dark mode support 🌙
- Clean dashboard-style interface
- Drag & drop habit ordering
- Progress visualization using charts and graphs

---

### 📊 Habit Detail Page
Each habit includes:
- Completion history
- Current & longest streak
- Average daily value
- Consistency score
- Performance charts (trend visualization)

---

### 🌍 Global Dashboard
- Overview of all habits
- Highlights:
  - Best performing habits
  - Weak performing habits
  - Overall discipline summary
- Aggregated consistency insights across all habits
