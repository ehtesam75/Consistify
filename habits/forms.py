from decimal import Decimal, ROUND_HALF_UP

from django import forms

from .models import Habit, HabitCategory


class HabitForm(forms.ModelForm):
    DAYS_OF_WEEK_CHOICES = [
        ("0", "Monday"),
        ("1", "Tuesday"),
        ("2", "Wednesday"),
        ("3", "Thursday"),
        ("4", "Friday"),
        ("5", "Saturday"),
        ("6", "Sunday"),
    ]

    days_of_week = forms.MultipleChoiceField(
        choices=DAYS_OF_WEEK_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "checkbox-grid"}),
        help_text="Only used for specific-day schedules.",
    )
    categories = forms.ModelMultipleChoiceField(
        queryset=HabitCategory.objects.none(),
        required=True,
        widget=forms.SelectMultiple(attrs={"class": "select select-multi", "size": 6}),
        help_text="Select up to three categories.",
    )
    tags = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "#morning, #focus, #fitness"}
        ),
        help_text="Use comma-separated tags.",
    )
    target_value = forms.DecimalField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(
            attrs={"class": "input", "min": 0, "step": "1"}
        ),
        help_text="Only used for quantitative habits.",
    )
    unit = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "glasses, hours, steps"}
        ),
        help_text="Only used for quantitative habits.",
    )

    class Meta:
        model = Habit
        fields = [
            "name",
            "description",
            "habit_type",
            "target_value",
            "unit",
            "schedule_type",
            "categories",
            "priority",
            "tags",
            "start_date",
            "interval_days",
            "weekly_interval",
            "days_of_week",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "textarea", "rows": 4}),
            "habit_type": forms.Select(attrs={"class": "select"}),
            "schedule_type": forms.Select(attrs={"class": "select"}),
            "priority": forms.Select(attrs={"class": "select"}),
            "start_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "interval_days": forms.NumberInput(attrs={"class": "input", "min": 1}),
            "weekly_interval": forms.NumberInput(attrs={"class": "input", "min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interval_days"].required = False
        self.fields["weekly_interval"].required = False
        self.fields["target_value"].required = False
        self.fields["unit"].required = False
        self.fields["categories"].queryset = HabitCategory.objects.all()
        if self.instance and self.instance.days_of_week:
            self.initial["days_of_week"] = self.instance.days_of_week.split(",")
        if self.instance and self.instance.tags:
            self.initial["tags"] = ", ".join(
                [f"#{tag}" for tag in self.instance.get_tags()]
            )
        # The start date is locked once a habit exists so historical
        # analytics, reports, and Consistify Scores stay accurate.
        if self.instance and self.instance.pk:
            start_field = self.fields["start_date"]
            start_field.disabled = True
            start_field.required = False
            start_field.help_text = (
                "Start date is locked after creation to preserve history."
            )
            start_field.widget.attrs["readonly"] = True

    def clean_start_date(self):
        # A disabled field always returns its initial value, but guard the
        # immutable start date explicitly so it can never change post-creation.
        if self.instance and self.instance.pk:
            return self.instance.start_date
        return self.cleaned_data.get("start_date")


    def clean(self):
        cleaned_data = super().clean()
        schedule_type = cleaned_data.get("schedule_type")
        interval_days = cleaned_data.get("interval_days") or 0
        weekly_interval = cleaned_data.get("weekly_interval") or 0
        days_of_week = cleaned_data.get("days_of_week") or []
        categories = cleaned_data.get("categories") or []
        habit_type = cleaned_data.get("habit_type")
        target_value = cleaned_data.get("target_value")
        unit = (cleaned_data.get("unit") or "").strip()

        if not categories:
            self.add_error("categories", "Select at least one category.")
        elif len(categories) > 3:
            self.add_error("categories", "Choose at most three categories.")

        if schedule_type == Habit.SCHEDULE_INTERVAL and interval_days < 1:
            self.add_error("interval_days", "Interval must be at least 1 day.")

        if schedule_type == Habit.SCHEDULE_WEEKLY and weekly_interval < 1:
            self.add_error("weekly_interval", "Weekly interval must be at least 1.")

        if schedule_type == Habit.SCHEDULE_DAYS and not days_of_week:
            self.add_error("days_of_week", "Select at least one day.")

        if habit_type == Habit.HABIT_QUANTITATIVE:
            if not target_value or target_value <= 0:
                self.add_error("target_value", "Target value must be greater than 0.")
            if not unit:
                self.add_error("unit", "Unit is required for quantitative habits.")
            if target_value:
                target_value = target_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        else:
            target_value = None
            unit = ""

        raw_tags = cleaned_data.get("tags", "")
        normalized_tags = []
        if raw_tags:
            for raw_tag in raw_tags.split(","):
                tag = raw_tag.strip().lstrip("#").lower()
                if tag:
                    normalized_tags.append(tag)
        cleaned_data["tags"] = ",".join(dict.fromkeys(normalized_tags))
        cleaned_data["unit"] = unit
        cleaned_data["target_value"] = target_value

        cleaned_data["days_of_week"] = ",".join(days_of_week) if days_of_week else ""
        return cleaned_data

    def save(self, commit=True):
        habit = super().save(commit=False)
        schedule_type = self.cleaned_data.get("schedule_type")

        habit.interval_days = habit.interval_days or 1
        habit.weekly_interval = habit.weekly_interval or 1

        if schedule_type != Habit.SCHEDULE_DAYS:
            habit.days_of_week = ""
        habit.tags = self.cleaned_data.get("tags", "")
        if habit.habit_type != Habit.HABIT_QUANTITATIVE:
            habit.target_value = None
            habit.unit = ""

        if commit:
            habit.save()
            self.save_m2m()
        return habit
