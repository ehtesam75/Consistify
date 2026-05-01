from django import forms

from .models import Habit


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

    class Meta:
        model = Habit
        fields = [
            "name",
            "description",
            "schedule_type",
            "start_date",
            "interval_days",
            "weekly_interval",
            "days_of_week",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "textarea", "rows": 4}),
            "schedule_type": forms.Select(attrs={"class": "select"}),
            "start_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "interval_days": forms.NumberInput(attrs={"class": "input", "min": 1}),
            "weekly_interval": forms.NumberInput(attrs={"class": "input", "min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interval_days"].required = False
        self.fields["weekly_interval"].required = False
        if self.instance and self.instance.days_of_week:
            self.initial["days_of_week"] = self.instance.days_of_week.split(",")

    def clean(self):
        cleaned_data = super().clean()
        schedule_type = cleaned_data.get("schedule_type")
        interval_days = cleaned_data.get("interval_days") or 0
        weekly_interval = cleaned_data.get("weekly_interval") or 0
        days_of_week = cleaned_data.get("days_of_week") or []

        if schedule_type == Habit.SCHEDULE_INTERVAL and interval_days < 1:
            self.add_error("interval_days", "Interval must be at least 1 day.")

        if schedule_type == Habit.SCHEDULE_WEEKLY and weekly_interval < 1:
            self.add_error("weekly_interval", "Weekly interval must be at least 1.")

        if schedule_type == Habit.SCHEDULE_DAYS and not days_of_week:
            self.add_error("days_of_week", "Select at least one day.")

        cleaned_data["days_of_week"] = ",".join(days_of_week) if days_of_week else ""
        return cleaned_data

    def save(self, commit=True):
        habit = super().save(commit=False)
        schedule_type = self.cleaned_data.get("schedule_type")

        habit.interval_days = habit.interval_days or 1
        habit.weekly_interval = habit.weekly_interval or 1

        if schedule_type != Habit.SCHEDULE_DAYS:
            habit.days_of_week = ""

        if commit:
            habit.save()
        return habit
