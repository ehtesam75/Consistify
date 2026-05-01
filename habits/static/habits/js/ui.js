window.ConsistifyUI = (() => {
    const THEME_KEY = "consistify-theme";
    const THEME_DARK = "dark";
    const THEME_LIGHT = "light";

    function readThemePreference() {
        try {
            return window.localStorage.getItem(THEME_KEY);
        } catch (error) {
            return null;
        }
    }

    function writeThemePreference(theme) {
        try {
            window.localStorage.setItem(THEME_KEY, theme);
        } catch (error) {
            // Storage may be blocked by browser privacy settings.
        }
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        const toggle = document.getElementById("themeToggle");
        if (toggle) {
            const label =
                theme === THEME_DARK ? "Switch to light mode" : "Switch to dark mode";
            toggle.setAttribute("aria-label", label);
            toggle.setAttribute("title", label);
        }
    }

    function initThemeToggle() {
        const stored = readThemePreference();
        if (stored === THEME_DARK || stored === THEME_LIGHT) {
            applyTheme(stored);
        } else {
            applyTheme(THEME_LIGHT);
        }

        const toggle = document.getElementById("themeToggle");
        if (!toggle) {
            return;
        }
        toggle.addEventListener("click", () => {
            const current = document.documentElement.getAttribute("data-theme");
            const nextTheme = current === THEME_DARK ? THEME_LIGHT : THEME_DARK;
            writeThemePreference(nextTheme);
            applyTheme(nextTheme);
        });
    }

    function getCsrfToken() {
        const cookie = document.cookie
            .split(";")
            .map((entry) => entry.trim())
            .find((entry) => entry.startsWith("csrftoken="));
        if (!cookie) {
            return "";
        }
        return decodeURIComponent(cookie.split("=")[1]);
    }

    function getDragAfterElement(container, y) {
        const elements = [...container.querySelectorAll(".reorder-item:not(.dragging)")];
        return elements.reduce(
            (closest, child) => {
                const box = child.getBoundingClientRect();
                const offset = y - box.top - box.height / 2;
                if (offset < 0 && offset > closest.offset) {
                    return { offset, element: child };
                }
                return closest;
            },
            { offset: Number.NEGATIVE_INFINITY, element: null }
        ).element;
    }

    function enableHabitDragSort(listId) {
        const list = document.getElementById(listId);
        if (!list) {
            return;
        }

        const endpoint = list.dataset.reorderUrl;
        if (!endpoint) {
            return;
        }

        let draggingItem = null;

        list.addEventListener("dragstart", (event) => {
            const item = event.target.closest(".reorder-item");
            if (!item) {
                return;
            }
            draggingItem = item;
            item.classList.add("dragging");
        });

        list.addEventListener("dragend", async () => {
            if (!draggingItem) {
                return;
            }
            draggingItem.classList.remove("dragging");
            draggingItem = null;

            const orderedIds = [...list.querySelectorAll(".reorder-item")].map((item) =>
                item.dataset.habitId
            );

            try {
                await fetch(endpoint, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "X-CSRFToken": getCsrfToken(),
                    },
                    body: new URLSearchParams({
                        habit_ids: orderedIds.join(","),
                    }),
                });
            } catch (error) {
                // Keep the UI responsive even if persistence fails.
                console.error("Failed to save habit order", error);
            }
        });

        list.addEventListener("dragover", (event) => {
            event.preventDefault();
            if (!draggingItem) {
                return;
            }
            const afterElement = getDragAfterElement(list, event.clientY);
            if (afterElement == null) {
                list.appendChild(draggingItem);
            } else {
                list.insertBefore(draggingItem, afterElement);
            }
        });
    }

    function parseNumber(value, fallback = 0) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function clampNumber(value, min, max) {
        return Math.min(Math.max(value, min), max);
    }

    function formatNumber(value, precision) {
        if (!Number.isFinite(value)) {
            return "0";
        }
        if (precision === 0 || Number.isInteger(value)) {
            return String(Math.round(value));
        }
        return value.toFixed(precision);
    }

    function submitForm(form) {
        if (!form) {
            return;
        }
        if (form.requestSubmit) {
            form.requestSubmit();
        } else {
            form.submit();
        }
    }

    function initHabitProgressControls() {
        const forms = document.querySelectorAll("[data-progress-form]");
        forms.forEach((form) => {
            const habitType = form.dataset.habitType;
            const targetValue = parseNumber(form.dataset.targetValue, 0);
            const unit = form.dataset.unit || "";
            const fill = form.querySelector("[data-progress-fill]");
            const percentLabel = form.querySelector("[data-progress-percent]");
            const caption = form.querySelector("[data-progress-caption]");
            let submitTimer = null;

            const scheduleSubmit = (delay = 350) => {
                if (submitTimer) {
                    window.clearTimeout(submitTimer);
                }
                submitTimer = window.setTimeout(() => submitForm(form), delay);
            };

            const updateVisual = (percent, rawValue) => {
                const safePercent = clampNumber(parseNumber(percent, 0), 0, 100);
                if (fill) {
                    fill.style.width = safePercent + "%";
                }
                if (percentLabel) {
                    percentLabel.textContent = Math.round(safePercent) + "%";
                }
                if (caption && habitType === "quantitative") {
                    const valueText = formatNumber(rawValue, 0);
                    const targetText = formatNumber(targetValue, 0);
                    caption.textContent = `${valueText} / ${targetText} ${unit}`.trim();
                }
            };

            if (habitType === "partial") {
                const range = form.querySelector("[data-progress-range]");
                const input = form.querySelector("[data-progress-input]");
                const sync = (value, shouldSubmit) => {
                    const percent = Math.round(
                        clampNumber(parseNumber(value, 0), 0, 100)
                    );
                    if (range) {
                        range.value = percent;
                    }
                    if (input) {
                        input.value = percent;
                    }
                    updateVisual(percent, percent);
                    if (shouldSubmit) {
                        scheduleSubmit();
                    }
                };

                if (range) {
                    range.addEventListener("input", (event) =>
                        sync(event.target.value, false)
                    );
                    range.addEventListener("change", (event) =>
                        sync(event.target.value, true)
                    );
                }
                if (input) {
                    input.addEventListener("input", (event) =>
                        sync(event.target.value, true)
                    );
                }
                const initial = input ? input.value : range ? range.value : 0;
                sync(initial, false);
                return;
            }

            if (habitType === "quantitative") {
                const input = form.querySelector("[data-quant-input]");
                const stepButtons = form.querySelectorAll("[data-step]");
                const stepValue = input ? parseNumber(input.step, 1) : 1;

                const update = (shouldSubmit) => {
                    if (!input) {
                        return;
                    }
                    const current = Math.round(parseNumber(input.value, 0));
                    input.value = current;
                    const percent = targetValue > 0 ? (current / targetValue) * 100 : 0;
                    updateVisual(percent, current);
                    if (shouldSubmit) {
                        scheduleSubmit(200);
                    }
                };

                stepButtons.forEach((button) => {
                    button.addEventListener("click", () => {
                        if (!input) {
                            return;
                        }
                        const delta = parseNumber(button.dataset.step, 0) * stepValue;
                        const current = parseNumber(input.value, 0) + delta;
                        const minValue = parseNumber(input.min, 0);
                        const maxValue = parseNumber(
                            input.max,
                            Number.POSITIVE_INFINITY
                        );
                        const nextValue = clampNumber(current, minValue, maxValue);
                        input.value = Math.round(nextValue);
                        update(true);
                    });
                });

                if (input) {
                    input.addEventListener("input", () => update(true));
                    update(false);
                }
                return;
            }

            if (habitType === "binary") {
                const checkbox = form.querySelector("[data-progress-checkbox]");
                const update = () => {
                    const percent = checkbox && checkbox.checked ? 100 : 0;
                    updateVisual(percent, percent);
                    submitForm(form);
                };
                if (checkbox) {
                    checkbox.addEventListener("change", update);
                    updateVisual(checkbox.checked ? 100 : 0, checkbox.checked ? 100 : 0);
                }
            }
        });
    }

    function initUiFeatures() {
        initThemeToggle();
        initHabitProgressControls();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initUiFeatures);
    } else {
        initUiFeatures();
    }

    return {
        enableHabitDragSort,
        initHabitProgressControls,
    };
})();
