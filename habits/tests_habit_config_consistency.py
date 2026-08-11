"""End-to-end verification that Create/Edit Habit and Today never disagree.

The habit's effective configuration is resolved from ``HabitPlanVersion`` by
``resolve_habit_plan_on``. These tests pin the contract that whatever the user
chooses in Create Habit or Edit Habit is exactly what Today renders once that
configuration is effective -- for every habit type, across every supported
transition, and through multiple consecutive effective-dated edits.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone


from .models import DEFAULT_CATEGORIES, Habit, HabitCategory, HabitPlanVersion
from .plan_versions import ensure_initial_plan_version, schedule_habit_plan_edit
from .services import compute_today_metrics, resolve_habit_plan_on


class HabitConfigConsistencyTests(TestCase):
    """Create/Edit choices must survive intact all the way to Today."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="config-user",
            password="pw",
        )
        self.categories = {}
        for index, (key, label) in enumerate(DEFAULT_CATEGORIES, start=1):
            category, _ = HabitCategory.objects.get_or_create(
                key=key,
                defaults={"label": label, "sort_order": index},
            )
            self.categories[key] = category
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=10)
        self.client.force_login(self.user)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _create_via_form(self, **overrides):
        """Create a habit through the real Create Habit view."""
        payload = {
            "name": "Habit",
            "description": "",
            "habit_type": Habit.HABIT_BINARY,
            "target_value": "",
            "unit": "",
            "schedule_type": Habit.SCHEDULE_DAILY,
            "categories": [self.categories["health"].pk],
            "priority": Habit.PRIORITY_MEDIUM,
            "tags": "",
            "start_date": self.start.isoformat(),
            "interval_days": 1,
            "weekly_interval": 1,
        }
        payload.update(overrides)
        response = self.client.post(reverse("habits:habit_create"), payload)
        self.assertEqual(response.status_code, 302, "create habit failed")
        return Habit.objects.get(user=self.user, name=payload["name"])

    def _today_row(self, habit, target_date):
        """Return the Today -> Scheduled Habits row for a habit on a date."""
        metrics = compute_today_metrics(self.user, target_date)
        return next(
            (row for row in metrics["rows"] if row["habit"].pk == habit.pk),
            None,
        )

    def _assert_today_matches_plan(self, habit, target_date, msg=""):
        """Today must render exactly the plan effective on ``target_date``."""
        habit = Habit.objects.prefetch_related(
            "pauses", "plan_versions__categories"
        ).get(pk=habit.pk)
        config = resolve_habit_plan_on(habit, target_date)
        row = self._today_row(habit, target_date)
        self.assertIsNotNone(row, f"habit missing from Today {msg}")

        # Today renders straight from the resolved plan, field for field.
        self.assertEqual(row["habit_type"], config.habit_type, msg)
        self.assertEqual(row["target_value"], config.target_value, msg)
        self.assertEqual(row["unit"], config.unit, msg)
        self.assertEqual(row["priority"], config.priority, msg)
        self.assertEqual(row["priority_label"], config.priority_label, msg)
        self.assertEqual(row["schedule_summary"], config.schedule_summary, msg)
        self.assertEqual(row["config"].category_ids, config.category_ids, msg)
        # The name is not versioned; it always comes from the habit itself.
        self.assertEqual(row["habit"].name, habit.name, msg)
        return row, config

    def _assert_today_shows(self, habit, target_date, expected, msg=""):
        """Today must show these exact user-chosen values."""
        row = self._today_row(habit, target_date)
        self.assertIsNotNone(row, f"habit missing from Today {msg}")
        for field, value in expected.items():
            self.assertEqual(row[field], value, f"{field} mismatch {msg}")
        return row

    # ------------------------------------------------------------------
    # 1. Create -> Today, for every habit type
    # ------------------------------------------------------------------
    def test_created_binary_habit_renders_on_today_as_chosen(self):
        habit = self._create_via_form(
            name="Read",
            habit_type=Habit.HABIT_BINARY,
            priority=Habit.PRIORITY_HIGH,
            categories=[self.categories["study"].pk],
        )
        self._assert_today_shows(
            habit,
            self.today,
            {
                "habit_type": Habit.HABIT_BINARY,
                "target_value": None,
                "unit": "",
                "priority": Habit.PRIORITY_HIGH,
                "schedule_summary": "Every day",
            },
            "(created binary)",
        )
        self._assert_today_matches_plan(habit, self.today, "(created binary)")

    def test_created_quantitative_habit_renders_on_today_as_chosen(self):
        habit = self._create_via_form(
            name="Water",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value="8",
            unit="glasses",
            priority=Habit.PRIORITY_LOW,
        )
        self._assert_today_shows(
            habit,
            self.today,
            {
                "habit_type": Habit.HABIT_QUANTITATIVE,
                "target_value": Decimal("8"),
                "unit": "glasses",
                "priority": Habit.PRIORITY_LOW,
            },
            "(created quantitative)",
        )
        self._assert_today_matches_plan(habit, self.today, "(created quantitative)")

    def test_created_partial_habit_renders_on_today_as_chosen(self):
        habit = self._create_via_form(
            name="Tidy",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAYS,
            days_of_week=["0", "1", "2", "3", "4", "5", "6"],
        )
        self._assert_today_shows(
            habit,
            self.today,
            {
                "habit_type": Habit.HABIT_PARTIAL,
                # A partial habit is scored 0-100%, so it carries no target.
                "target_value": None,
                "unit": "",
            },
            "(created partial)",
        )
        self._assert_today_matches_plan(habit, self.today, "(created partial)")

    # ------------------------------------------------------------------
    # 2. Every supported habit-type transition
    # ------------------------------------------------------------------
    def _make(self, habit_type, **overrides):
        fields = {
            "user": self.user,
            "name": f"{habit_type} habit",
            "habit_type": habit_type,
            "schedule_type": Habit.SCHEDULE_DAILY,
            "priority": Habit.PRIORITY_MEDIUM,
            "start_date": self.start,
        }
        if habit_type == Habit.HABIT_QUANTITATIVE:
            fields["target_value"] = Decimal("10")
            fields["unit"] = "reps"
        fields.update(overrides)
        habit = Habit.objects.create(**fields)
        habit.categories.set([self.categories["health"]])
        ensure_initial_plan_version(habit)
        return habit

    def test_every_habit_type_transition_lands_exactly_on_today(self):
        """All six transitions must be honoured, and only from the effective date."""
        transitions = [
            (Habit.HABIT_BINARY, Habit.HABIT_QUANTITATIVE, Decimal("5"), "km"),
            (Habit.HABIT_BINARY, Habit.HABIT_PARTIAL, None, ""),
            (Habit.HABIT_QUANTITATIVE, Habit.HABIT_BINARY, None, ""),
            (Habit.HABIT_QUANTITATIVE, Habit.HABIT_PARTIAL, None, ""),
            (Habit.HABIT_PARTIAL, Habit.HABIT_BINARY, None, ""),
            (Habit.HABIT_PARTIAL, Habit.HABIT_QUANTITATIVE, Decimal("3"), "cups"),
        ]

        for source, target, target_value, unit in transitions:
            with self.subTest(transition=f"{source}->{target}"):
                habit = self._make(source, name=f"{source} to {target}")

                schedule_habit_plan_edit(
                    habit,
                    habit_type=target,
                    target_value=target_value,
                    unit=unit,
                    today=self.today,
                )

                # Before the effective date the old type still governs.
                row_today = self._today_row(habit, self.today)
                self.assertEqual(
                    row_today["habit_type"],
                    source,
                    "a scheduled edit changed the type before its effective date",
                )

                # From the effective date the new type governs exactly.
                effective = self.today + timedelta(days=1)
                self._assert_today_shows(
                    habit,
                    effective,
                    {
                        "habit_type": target,
                        "target_value": target_value,
                        "unit": unit,
                    },
                    f"({source}->{target})",
                )
                self._assert_today_matches_plan(habit, effective)

    def test_switching_to_quantitative_requires_a_target(self):
        """A type change may not leave the habit unscoreable."""
        habit = self._make(Habit.HABIT_BINARY, name="No target")
        with self.assertRaises(ValidationError):
            schedule_habit_plan_edit(
                habit,
                habit_type=Habit.HABIT_QUANTITATIVE,
                today=self.today,
            )

    # ------------------------------------------------------------------
    # 3. Field-level edits preserve everything untouched
    # ------------------------------------------------------------------
    def test_target_and_unit_edits_apply_without_touching_other_fields(self):
        habit = self._make(
            Habit.HABIT_QUANTITATIVE,
            name="Run",
            priority=Habit.PRIORITY_HIGH,
        )
        schedule_habit_plan_edit(
            habit, target_value=Decimal("20"), unit="minutes", today=self.today
        )

        effective = self.today + timedelta(days=1)
        row = self._assert_today_shows(
            habit,
            effective,
            {
                "habit_type": Habit.HABIT_QUANTITATIVE,
                "target_value": Decimal("20"),
                "unit": "minutes",
                # Untouched fields survive the edit.
                "priority": Habit.PRIORITY_HIGH,
                "schedule_summary": "Every day",
            },
            "(target+unit edit)",
        )
        self.assertEqual(
            row["config"].category_ids,
            frozenset({self.categories["health"].pk}),
        )

    def test_priority_category_and_schedule_edits_preserve_quantity_fields(self):
        habit = self._make(Habit.HABIT_QUANTITATIVE, name="Study")
        schedule_habit_plan_edit(
            habit,
            priority=Habit.PRIORITY_LOW,
            categories=[self.categories["study"], self.categories["spiritual"]],
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=1,
            today=self.today,
        )

        effective = self.today + timedelta(days=1)
        row = self._assert_today_shows(
            habit,
            effective,
            {
                "habit_type": Habit.HABIT_QUANTITATIVE,
                "target_value": Decimal("10"),
                "unit": "reps",
                "priority": Habit.PRIORITY_LOW,
            },
            "(priority/category/schedule edit)",
        )
        self.assertEqual(
            row["config"].category_ids,
            frozenset(
                {self.categories["study"].pk, self.categories["spiritual"].pk}
            ),
        )

    def test_multiple_consecutive_edits_accumulate_exactly(self):
        """Each edit changes only its own field; earlier ones survive."""
        habit = self._make(Habit.HABIT_QUANTITATIVE, name="Layered")

        schedule_habit_plan_edit(
            habit, priority=Habit.PRIORITY_HIGH, today=self.today
        )
        schedule_habit_plan_edit(
            habit, target_value=Decimal("42"), today=self.today + timedelta(days=1)
        )
        schedule_habit_plan_edit(
            habit,
            categories=[self.categories["organize"]],
            today=self.today + timedelta(days=2),
        )
        schedule_habit_plan_edit(
            habit, unit="pages", today=self.today + timedelta(days=3)
        )

        final_date = self.today + timedelta(days=4)
        row = self._assert_today_shows(
            habit,
            final_date,
            {
                "habit_type": Habit.HABIT_QUANTITATIVE,
                "target_value": Decimal("42"),
                "unit": "pages",
                "priority": Habit.PRIORITY_HIGH,
            },
            "(four consecutive edits)",
        )
        self.assertEqual(
            row["config"].category_ids,
            frozenset({self.categories["organize"].pk}),
        )
        self._assert_today_matches_plan(habit, final_date, "(four consecutive edits)")

    # ------------------------------------------------------------------
    # 4. Edit Habit page vs Today/history agreement
    # ------------------------------------------------------------------
    def test_edit_form_initial_matches_today_for_the_effective_date(self):
        """What Edit Habit shows must equal what Today renders once effective."""
        habit = self._make(Habit.HABIT_QUANTITATIVE, name="Agree")
        response = self.client.post(
            reverse("habits:habit_edit", args=[habit.pk]),
            {
                "name": "Agree",
                "description": "",
                "habit_type": Habit.HABIT_QUANTITATIVE,
                "target_value": "12",
                "unit": "laps",
                "schedule_type": Habit.SCHEDULE_DAILY,
                "categories": [self.categories["health"].pk],
                "priority": Habit.PRIORITY_LOW,
                "tags": "",
                "interval_days": 1,
                "weekly_interval": 1,
            },
        )
        self.assertEqual(response.status_code, 302)

        effective = self.today + timedelta(days=1)
        form = self.client.get(
            reverse("habits:habit_edit", args=[habit.pk])
        ).context["form"]
        row, config = self._assert_today_matches_plan(habit, effective)

        # The Edit Habit form and the effective plan describe the same habit.
        self.assertEqual(form.initial["habit_type"], config.habit_type)
        self.assertEqual(Decimal(str(form.initial["target_value"])), config.target_value)
        self.assertEqual(form.initial["unit"], config.unit)
        self.assertEqual(form.initial["priority"], config.priority)
        self.assertEqual(row["habit_type"], form.initial["habit_type"])

    def test_history_and_today_resolve_their_own_dates_independently(self):
        """A past date keeps its own plan; only the new date moves."""
        habit = self._make(Habit.HABIT_QUANTITATIVE, name="Timeline")
        schedule_habit_plan_edit(
            habit,
            habit_type=Habit.HABIT_PARTIAL,
            today=self.today,
        )

        habit = Habit.objects.prefetch_related(
            "pauses", "plan_versions__categories"
        ).get(pk=habit.pk)

        past = resolve_habit_plan_on(habit, self.start + timedelta(days=1))
        self.assertEqual(past.habit_type, Habit.HABIT_QUANTITATIVE)
        self.assertEqual(past.target_value, Decimal("10"))

        future = resolve_habit_plan_on(habit, self.today + timedelta(days=1))
        self.assertEqual(future.habit_type, Habit.HABIT_PARTIAL)
        self.assertIsNone(future.target_value)


class PendingEffectiveDatedEditTests(TestCase):
    """A pending edit must not leak into Today before its effective date.

    Edit Habit and Today deliberately answer *different* questions:

    * Edit Habit shows the newest saved intent (what will apply going forward).
    * Today shows the configuration effective on the date being viewed.

    So immediately after an edit the two legitimately disagree, and that is
    correct behaviour rather than the reclassification bug. These tests pin
    the switchover to the exact day boundary.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="pending-user",
            password="pw",
        )
        self.categories = {}
        for index, (key, label) in enumerate(DEFAULT_CATEGORIES, start=1):
            category, _ = HabitCategory.objects.get_or_create(
                key=key,
                defaults={"label": label, "sort_order": index},
            )
            self.categories[key] = category

        self.today = timezone.localdate()
        self.tomorrow = self.today + timedelta(days=1)
        self.start = self.today - timedelta(days=10)
        self.client.force_login(self.user)

        self.habit = Habit.objects.create(
            user=self.user,
            name="Water",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
            unit="glasses",
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
            start_date=self.start,
        )
        self.habit.categories.set([self.categories["health"]])
        ensure_initial_plan_version(self.habit)

    def _today_page_row(self, as_of):
        """Render the real Today page with the clock pinned to ``as_of``."""
        with patch("django.utils.timezone.localdate", return_value=as_of):
            response = self.client.get(reverse("habits:today"))

        self.assertEqual(response.status_code, 200)
        rows = response.context["scheduled_habits"]
        return next(
            (row for row in rows if row["habit"].pk == self.habit.pk), None
        )

    def _edit_form_initial(self):
        response = self.client.get(
            reverse("habits:habit_edit", args=[self.habit.pk])
        )
        self.assertEqual(response.status_code, 200)
        return response.context["form"].initial

    def test_pending_type_change_applies_only_from_its_effective_date(self):
        """Quantitative today, Partial from tomorrow -- switching exactly then."""
        schedule_habit_plan_edit(
            self.habit, habit_type=Habit.HABIT_PARTIAL, today=self.today
        )

        # 3. Today still renders today's effective configuration.
        row_today = self._today_page_row(self.today)
        self.assertEqual(row_today["habit_type"], Habit.HABIT_QUANTITATIVE)
        self.assertEqual(row_today["target_value"], Decimal("10"))
        self.assertEqual(row_today["unit"], "glasses")

        # 4. Edit Habit may already show the pending intent.
        self.assertEqual(
            self._edit_form_initial()["habit_type"], Habit.HABIT_PARTIAL
        )

        # 5. The switch happens exactly at the effective date: not the day
        # before, and it stays switched the day after.
        row_tomorrow = self._today_page_row(self.tomorrow)
        self.assertEqual(row_tomorrow["habit_type"], Habit.HABIT_PARTIAL)
        self.assertIsNone(row_tomorrow["target_value"])

        row_after = self._today_page_row(self.tomorrow + timedelta(days=1))
        self.assertEqual(row_after["habit_type"], Habit.HABIT_PARTIAL)

        # History before the edit is untouched.
        row_yesterday = self._today_page_row(self.today - timedelta(days=1))
        self.assertEqual(row_yesterday["habit_type"], Habit.HABIT_QUANTITATIVE)

    def test_pending_edits_to_other_fields_also_wait_for_the_effective_date(self):
        """The same boundary rule governs priority, schedule, target, unit, category."""
        cases = [
            (
                "priority",
                {"priority": Habit.PRIORITY_HIGH},
                "priority",
                Habit.PRIORITY_MEDIUM,
                Habit.PRIORITY_HIGH,
            ),
            (
                "target_value",
                {"target_value": Decimal("25")},
                "target_value",
                Decimal("10"),
                Decimal("25"),
            ),
            (
                "unit",
                {"unit": "litres"},
                "unit",
                "glasses",
                "litres",
            ),
            (
                "schedule",
                {"schedule_type": Habit.SCHEDULE_INTERVAL, "interval_days": 3},
                "schedule_summary",
                "Every day",
                "Every 3 days",
            ),
        ]

        for label, edit_kwargs, field, before, after in cases:
            with self.subTest(field=label):
                # Each case edits a habit of its own so the assertions stay
                # independent of one another.
                habit = Habit.objects.create(
                    user=self.user,
                    name=f"Habit {label}",
                    habit_type=Habit.HABIT_QUANTITATIVE,
                    target_value=Decimal("10"),
                    unit="glasses",
                    schedule_type=Habit.SCHEDULE_DAILY,
                    priority=Habit.PRIORITY_MEDIUM,
                    start_date=self.start,
                )
                habit.categories.set([self.categories["health"]])
                ensure_initial_plan_version(habit)

                schedule_habit_plan_edit(habit, today=self.today, **edit_kwargs)

                stored = Habit.objects.prefetch_related(
                    "pauses", "plan_versions__categories"
                ).get(pk=habit.pk)

                today_config = resolve_habit_plan_on(stored, self.today)
                self.assertEqual(
                    getattr(today_config, field),
                    before,
                    f"{label} changed before its effective date",
                )

                tomorrow_config = resolve_habit_plan_on(stored, self.tomorrow)
                self.assertEqual(
                    getattr(tomorrow_config, field),
                    after,
                    f"{label} did not apply on its effective date",
                )

                # The habit type is never collateral damage of these edits.
                self.assertEqual(
                    tomorrow_config.habit_type, Habit.HABIT_QUANTITATIVE
                )

    def test_pending_category_change_waits_for_the_effective_date(self):
        schedule_habit_plan_edit(
            self.habit,
            categories=[self.categories["spiritual"]],
            today=self.today,
        )
        stored = Habit.objects.prefetch_related(
            "pauses", "plan_versions__categories"
        ).get(pk=self.habit.pk)

        self.assertEqual(
            resolve_habit_plan_on(stored, self.today).category_ids,
            frozenset({self.categories["health"].pk}),
        )
        self.assertEqual(
            resolve_habit_plan_on(stored, self.tomorrow).category_ids,
            frozenset({self.categories["spiritual"].pk}),
        )

    def test_progress_logging_uses_todays_type_while_an_edit_is_pending(self):
        """The pending Partial edit must not change how today is logged."""
        schedule_habit_plan_edit(
            self.habit, habit_type=Habit.HABIT_PARTIAL, today=self.today
        )

        # Today is still quantitative, so a raw value is what gets recorded.
        with patch("django.utils.timezone.localdate", return_value=self.today):
            response = self.client.post(
                reverse("habits:update_progress", args=[self.habit.pk]),
                {"current_value": "5", "date": self.today.isoformat()},
            )
        self.assertIn(response.status_code, (200, 302))

        completion = self.habit.habitcompletion_set.get(date=self.today)
        self.assertEqual(completion.raw_value, Decimal("5.00"))
        self.assertEqual(completion.completion_percentage, Decimal("50.00"))


class LegacyBinaryAmbiguityTests(TestCase):

    """Document exactly which corrupted rows can and cannot be recovered."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="legacy-user",
            password="pw",
        )
        self.category, _ = HabitCategory.objects.get_or_create(
            key="health",
            defaults={"label": "Health", "sort_order": 1},
        )
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=10)

        self.habit = Habit.objects.create(
            user=self.user,
            name="Water",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
            unit="glasses",
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
            start_date=self.start,
        )
        self.habit.categories.set([self.category])
        ensure_initial_plan_version(self.habit)

    def _corrupt(self, stored_type):
        version = HabitPlanVersion.objects.create(
            habit=self.habit,
            effective_from=self.start + timedelta(days=2),
            schedule_anchor=self.start,
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
        )
        HabitPlanVersion.objects.filter(pk=version.pk).update(
            habit_type=stored_type, target_value=None, unit=""
        )
        habit = Habit.objects.prefetch_related(
            "pauses", "plan_versions__categories"
        ).get(pk=self.habit.pk)
        return resolve_habit_plan_on(habit, self.start + timedelta(days=3))

    def test_backdated_row_inherits_the_type_effective_on_its_own_date(self):
        """Inheritance must use the plan effective then, not the newest one.

        The base ``Habit`` mirrors the newest configuration, including an edit
        that is not effective yet. A row inserted for a *past* date must
        inherit what the habit actually was on that date.
        """
        # A pending edit flips the habit to binary from tomorrow. The base
        # Habit record now already reads "binary".
        schedule_habit_plan_edit(
            self.habit, habit_type=Habit.HABIT_BINARY, today=self.today
        )
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.habit_type, Habit.HABIT_BINARY)

        # A back-dated row with no type must not adopt that future binary
        # value; on its own date the habit was still quantitative.
        backdated = HabitPlanVersion.objects.create(
            habit=self.habit,
            effective_from=self.start + timedelta(days=1),
            schedule_anchor=self.start,
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
        )
        backdated.refresh_from_db()
        self.assertEqual(backdated.habit_type, Habit.HABIT_QUANTITATIVE)
        self.assertEqual(backdated.target_value, Decimal("10"))
        self.assertEqual(backdated.unit, "glasses")

    def test_blank_stored_type_is_recovered_at_read_time(self):

        """A blank type is unambiguously "never written" and is repaired."""
        config = self._corrupt("")
        self.assertEqual(config.habit_type, Habit.HABIT_QUANTITATIVE)
        self.assertEqual(config.target_value, Decimal("10"))
        self.assertEqual(config.unit, "glasses")

    def test_literal_binary_is_indistinguishable_from_a_real_choice(self):
        """A stored "binary" is taken at face value -- by design.

        ``_habit_plan_configs`` only rescues a *blank* type. A row holding the
        literal string "binary" is honoured as a deliberate Binary plan,
        because an intentional Quantitative -> Binary edit stores exactly that
        value and must not be silently reverted. Rows that migration 0010
        defaulted to "binary" are therefore repaired by the timestamp-scoped
        backfill in migration 0013, not at read time.
        """
        config = self._corrupt(Habit.HABIT_BINARY)
        self.assertEqual(config.habit_type, Habit.HABIT_BINARY)

        # An intentional Binary edit produces the same stored value, which is
        # precisely why read-time repair cannot be used to tell them apart.
        schedule_habit_plan_edit(
            self.habit, habit_type=Habit.HABIT_BINARY, today=self.today
        )
        intentional = HabitPlanVersion.objects.get(
            habit=self.habit, effective_from=self.today + timedelta(days=1)
        )
        self.assertEqual(intentional.habit_type, Habit.HABIT_BINARY)
