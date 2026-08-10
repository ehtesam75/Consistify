window.ConsistifyUI = (() => {
    const THEME_KEY = "consistify-theme";
    const THEME_DARK = "dark";
    const THEME_LIGHT = "light";
    const THEME_COLORS = {
        light: "#c29b57",
        dark: "#111827",
    };

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

    function updateThemeColorMeta(theme) {
        const meta = document.getElementById("theme-color-meta");
        if (meta) {
            meta.setAttribute("content", theme === THEME_DARK ? THEME_COLORS.dark : THEME_COLORS.light);
        }
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        updateThemeColorMeta(theme);
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
        let touchCandidate = null;
        let touchStartX = 0;
        let touchStartY = 0;
        let touchDragging = false;
        let suppressClick = false;
        let touchHoldTimer = null;
        const isCoarsePointer = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;

        const persistOrder = async () => {
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
        };

        const startDragging = (item) => {
            draggingItem = item;
            draggingItem.classList.add("dragging");
        };

        const clearTouchHold = () => {
            if (touchHoldTimer) {
                window.clearTimeout(touchHoldTimer);
                touchHoldTimer = null;
            }
        };

        const finishDragging = () => {
            if (!draggingItem) {
                return;
            }
            draggingItem.classList.remove("dragging");
            draggingItem = null;
            touchDragging = false;
            void persistOrder();
        };

        const moveDragging = (clientY) => {
            if (!draggingItem) {
                return;
            }
            const afterElement = getDragAfterElement(list, clientY);
            if (afterElement == null) {
                list.appendChild(draggingItem);
            } else {
                list.insertBefore(draggingItem, afterElement);
            }
        };

        list.addEventListener("dragstart", (event) => {
            if (event.target.closest("[data-no-drag]")) {
                event.preventDefault();
                return;
            }
            const item = event.target.closest(".reorder-item");
            if (!item || draggingItem) {
                return;
            }
            startDragging(item);
        });

        list.addEventListener("dragend", () => {
            if (!draggingItem) {
                return;
            }
            finishDragging();
        });

        list.addEventListener("dragover", (event) => {
            event.preventDefault();
            moveDragging(event.clientY);
        });

        if (isCoarsePointer) {
            list.addEventListener(
                "selectstart",
                (event) => {
                    if (!event.target.closest(".reorder-item") || event.target.closest("[data-no-drag]")) {
                        return;
                    }
                    event.preventDefault();
                },
                true
            );

            list.addEventListener(
                "contextmenu",
                (event) => {
                    if (!event.target.closest(".reorder-item") || event.target.closest("[data-no-drag]")) {
                        return;
                    }
                    event.preventDefault();
                },
                true
            );
        }

        list.addEventListener(
            "touchstart",
            (event) => {
                if (event.target.closest("[data-no-drag]")) {
                    return;
                }
                const item = event.target.closest(".reorder-item");
                if (!item || event.touches.length !== 1) {
                    return;
                }
                touchCandidate = item;
                touchStartX = event.touches[0].clientX;
                touchStartY = event.touches[0].clientY;
                clearTouchHold();
                touchHoldTimer = window.setTimeout(() => {
                    if (!touchCandidate) {
                        touchHoldTimer = null;
                        return;
                    }
                    startDragging(touchCandidate);
                    touchDragging = true;
                    touchCandidate = null;
                    touchHoldTimer = null;
                }, 180);
            },
            { passive: true }
        );

        list.addEventListener(
            "touchmove",
            (event) => {
                if ((!touchCandidate && !draggingItem) || event.touches.length !== 1) {
                    return;
                }
                const touch = event.touches[0];
                if (draggingItem) {
                    event.preventDefault();
                    moveDragging(touch.clientY);
                    return;
                }
                const deltaX = Math.abs(touch.clientX - touchStartX);
                const deltaY = Math.abs(touch.clientY - touchStartY);
                if (Math.max(deltaX, deltaY) > 6) {
                    clearTouchHold();
                    touchCandidate = null;
                }
            },
            { passive: false }
        );

        const endTouchDrag = () => {
            if (touchDragging) {
                suppressClick = true;
                window.setTimeout(() => {
                    suppressClick = false;
                }, 400);
            }
            clearTouchHold();
            touchCandidate = null;
            if (draggingItem) {
                finishDragging();
            }
        };

        list.addEventListener("touchend", endTouchDrag);
        list.addEventListener("touchcancel", endTouchDrag);

        list.addEventListener(
            "click",
            (event) => {
                if (!suppressClick) {
                    return;
                }
                event.preventDefault();
                event.stopPropagation();
                suppressClick = false;
            },
            true
        );
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
        // Trim trailing zeros so 10.50 shows as 10.5 and 10.00 as 10.
        return value.toFixed(precision).replace(/\.?0+$/, "");
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

    const pendingProgressFlushers = new Set();
    let progressFlushListenersBound = false;

    function flushPendingProgressSaves() {
        pendingProgressFlushers.forEach((flush) => flush());
    }

    function bindProgressFlushListeners() {
        if (progressFlushListenersBound) {
            return;
        }
        progressFlushListenersBound = true;
        // A debounced save must not be dropped when the user leaves the page
        // (or backgrounds the tab) before the timer fires.
        window.addEventListener("pagehide", flushPendingProgressSaves);
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "hidden") {
                flushPendingProgressSaves();
            }
        });
    }

    function initDailyRecapQuantControls() {
        const form = document.querySelector(".daily-recap-form");
        if (!form) {
            return;
        }

        form.addEventListener("click", (event) => {
            const button = event.target.closest("[data-step]");
            if (!button || !form.contains(button)) {
                return;
            }

            const quantRow = button.closest(".daily-recap-quant");
            const input = quantRow ? quantRow.querySelector("[data-quant-input]") : null;
            if (!input || input.disabled) {
                return;
            }

            event.preventDefault();

            const step = parseNumber(button.dataset.step, 0);
            const minValue = parseNumber(input.min, 0);
            const maxValue = parseNumber(input.max, Number.POSITIVE_INFINITY);
            const currentValue = parseNumber(input.value, 0);
            const nextValue = clampNumber(currentValue + step, minValue, maxValue);

            input.value = formatNumber(Math.round(nextValue * 100) / 100, 2);
            // Mirror a real user edit: "input" for live listeners and "change"
            // for the listeners that commit/persist the value.
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
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
            const usesBlurAutosave =
                habitType === "partial" || habitType === "quantitative";

            if (usesBlurAutosave) {
                form.addEventListener("submit", (event) => {
                    event.preventDefault();
                });
            }

            const updateSummaryCounter = (data) => {
                if (data.completion_rate == null) {
                    return;
                }
                const summaries = document.querySelectorAll(".scheduled-summary-desktop");
                const rate = parseNumber(data.completion_rate, 0).toFixed(1);
                summaries.forEach((el) => {
                    el.textContent = rate + "% completed";
                });
            };

            const fetchSubmit = async ({ keepalive = false } = {}) => {
                const formData = new FormData(form);
                try {
                    const response = await fetch(form.action, {
                        method: "POST",
                        headers: {
                            "X-CSRFToken": getCsrfToken(),
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        body: formData,
                        keepalive,
                    });
                    if (response.ok) {
                        const data = await response.json();
                        updateSummaryCounter(data);
                    }
                } catch (error) {
                    console.error("Failed to save habit progress", error);
                }
            };

            let pendingSubmitId = null;
            const queueSubmit = () => {
                if (pendingSubmitId) {
                    window.clearTimeout(pendingSubmitId);
                }
                pendingSubmitId = window.setTimeout(() => {
                    pendingSubmitId = null;
                    fetchSubmit();
                }, 400);
            };

            const flushSubmit = () => {
                if (!pendingSubmitId) {
                    return;
                }
                window.clearTimeout(pendingSubmitId);
                pendingSubmitId = null;
                // keepalive lets the request outlive the page during navigation.
                fetchSubmit({ keepalive: true });
            };

            if (usesBlurAutosave) {
                pendingProgressFlushers.add(flushSubmit);
                bindProgressFlushListeners();
            }

            const updateVisual = (percent, rawValue) => {
                const safePercent = clampNumber(parseNumber(percent, 0), 0, 100);
                if (fill) {
                    fill.style.width = safePercent + "%";
                }
                if (percentLabel) {
                    percentLabel.textContent = Math.round(safePercent) + "%";
                }
                if (caption && habitType === "quantitative") {
                    const valueText = formatNumber(rawValue, 2);
                    const targetText = formatNumber(targetValue, 2);
                    caption.textContent = `${valueText} / ${targetText} ${unit}`.trim();
                }

            };

            if (habitType === "partial") {
                const range = form.querySelector("[data-progress-range]");
                const input = form.querySelector("[data-progress-input]");
                const sync = (value) => {
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
                    return percent;
                };
                let committedPercent = sync(
                    input ? input.value : range ? range.value : 0
                );
                const commitPercent = (value) => {
                    const nextPercent = sync(value);
                    if (nextPercent !== committedPercent) {
                        committedPercent = nextPercent;
                        queueSubmit();
                    }
                };

                if (range) {
                    range.addEventListener("input", (event) =>
                        sync(event.target.value)
                    );
                    range.addEventListener("change", (event) =>
                        commitPercent(event.target.value)
                    );
                }
                if (input) {
                    // Single commit path shared by typing, spinner buttons,
                    // arrow keys and pastes.
                    const commitFromInput = () => {
                        const rawValue = input.value.trim();
                        if (rawValue === "") {
                            sync(committedPercent);
                            return;
                        }
                        const enteredValue = Number(rawValue);
                        if (!Number.isFinite(enteredValue)) {
                            sync(committedPercent);
                            return;
                        }
                        commitPercent(enteredValue);
                    };

                    input.addEventListener("input", (event) =>
                        sync(event.target.value)
                    );
                    // The browser fires "change" immediately for the spinner
                    // buttons and arrow keys, and on commit for typed values,
                    // so every edit persists through the same logic.
                    input.addEventListener("change", commitFromInput);
                    input.addEventListener("keydown", (event) => {
                        if (event.key === "Enter") {
                            event.preventDefault();
                            commitFromInput();
                        }
                    });
                    input.addEventListener("blur", commitFromInput);
                }
                return;
            }

            if (habitType === "quantitative") {
                const input = form.querySelector("[data-quant-input]");
                if (!input) {
                    return;
                }
                const stepButtons = form.querySelectorAll("[data-step]");
                const stepValue = parseNumber(input.step, 1);
                const minValue = parseNumber(input.min, 0);
                const maxValue = parseNumber(
                    input.max,
                    Number.POSITIVE_INFINITY
                );

                const sync = (value) => {
                    // Round to at most two decimals so decimal targets
                    // (e.g. 10.5) are supported without float noise.
                    const clamped = clampNumber(
                        parseNumber(value, 0),
                        minValue,
                        maxValue
                    );
                    const current = Math.round(clamped * 100) / 100;
                    input.value = formatNumber(current, 2);
                    const percent = targetValue > 0 ? (current / targetValue) * 100 : 0;
                    updateVisual(percent, current);
                    return current;
                };

                let committedValue = sync(input.value);
                const commitValue = (value) => {
                    const nextValue = sync(value);
                    if (nextValue !== committedValue) {
                        committedValue = nextValue;
                        queueSubmit();
                    }
                };

                stepButtons.forEach((button) => {
                    button.addEventListener("click", () => {
                        const delta = parseNumber(button.dataset.step, 0) * stepValue;
                        const current = parseNumber(input.value, 0) + delta;
                        const nextValue = clampNumber(current, minValue, maxValue);
                        commitValue(nextValue);
                    });
                });

                // Single commit path shared by typing, the +/- step buttons,
                // the native spinner buttons and arrow keys.
                const commitFromInput = () => {
                    const rawValue = input.value.trim();
                    if (rawValue === "") {
                        sync(committedValue);
                        return;
                    }
                    const enteredValue = Number(rawValue);
                    if (!Number.isFinite(enteredValue)) {
                        sync(committedValue);
                        return;
                    }
                    commitValue(enteredValue);
                };

                input.addEventListener("input", () => sync(input.value));
                // The browser fires "change" immediately for the spinner
                // buttons and arrow keys, and on commit for typed values, so
                // every edit persists through the same logic.
                input.addEventListener("change", commitFromInput);
                input.addEventListener("keydown", (event) => {
                    if (event.key === "Enter") {
                        event.preventDefault();
                        commitFromInput();
                    }
                });
                input.addEventListener("blur", commitFromInput);
                return;
            }

            if (habitType === "binary") {
                form.addEventListener("submit", (event) => {
                    event.preventDefault();
                });
                const checkbox = form.querySelector("[data-progress-checkbox]");
                const update = () => {
                    const percent = checkbox && checkbox.checked ? 100 : 0;
                    updateVisual(percent, percent);
                    fetchSubmit();
                };
                if (checkbox) {
                    checkbox.addEventListener("change", update);
                    updateVisual(checkbox.checked ? 100 : 0, checkbox.checked ? 100 : 0);
                }
            }
        });
    }

    function initDeleteConfirmations() {
        const deleteForms = document.querySelectorAll("[data-confirm-delete]");
        deleteForms.forEach((form) => {
            form.addEventListener("submit", (event) => {
                const message =
                    form.dataset.confirmMessage ||
                    "Delete this habit? This cannot be undone.";
                if (!window.confirm(message)) {
                    event.preventDefault();
                }
            });
        });
    }

    function initNotificationMenus() {
        const menus = document.querySelectorAll(".notification-menu");
        if (!menus.length) {
            return;
        }

        document.addEventListener("click", (event) => {
            menus.forEach((menu) => {
                if (!menu.open) {
                    return;
                }
                if (menu.contains(event.target)) {
                    return;
                }
                menu.open = false;
            });
        });
    }

    function initScoreDriverHelp() {
        const helpItems = Array.from(document.querySelectorAll(".score-driver-help"));
        if (!helpItems.length) {
            return;
        }

        const closeHelp = (item) => {
            item.classList.remove("is-open");
            const button = item.querySelector("[data-score-driver-help]");
            if (button) {
                button.setAttribute("aria-expanded", "false");
            }
        };

        helpItems.forEach((item) => {
            const button = item.querySelector("[data-score-driver-help]");
            if (!button) {
                return;
            }

            button.addEventListener("click", (event) => {
                event.stopPropagation();
                const willOpen = !item.classList.contains("is-open");
                helpItems.forEach(closeHelp);
                if (willOpen) {
                    item.classList.add("is-open");
                    button.setAttribute("aria-expanded", "true");
                }
            });
        });

        document.addEventListener("click", (event) => {
            helpItems.forEach((item) => {
                if (!item.contains(event.target)) {
                    closeHelp(item);
                }
            });
        });

        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") {
                return;
            }
            helpItems.forEach(closeHelp);
        });
    }

    function initMobileNav() {
        const toggle = document.querySelector("[data-nav-toggle]");
        const panel = document.querySelector("[data-nav-panel]");
        const backdrop = document.querySelector("[data-nav-backdrop]");
        const sheet = panel ? panel.querySelector(".nav-mobile-sheet") : null;
        if (!toggle || !panel || !backdrop) {
            return;
        }

        const openLabel = toggle.getAttribute("aria-label") || "Open menu";
        const closeLabel = "Close menu";

        const openMenu = () => {
            document.body.classList.add("nav-open");
            toggle.setAttribute("aria-expanded", "true");
            toggle.setAttribute("aria-label", closeLabel);
            panel.setAttribute("aria-hidden", "false");
        };

        const closeMenu = () => {
            document.body.classList.remove("nav-open");
            toggle.setAttribute("aria-expanded", "false");
            toggle.setAttribute("aria-label", openLabel);
            panel.setAttribute("aria-hidden", "true");
        };

        toggle.addEventListener("click", () => {
            if (document.body.classList.contains("nav-open")) {
                closeMenu();
            } else {
                openMenu();
            }
        });

        backdrop.addEventListener("click", closeMenu);

        panel.addEventListener("click", (event) => {
            if (sheet && !sheet.contains(event.target)) {
                closeMenu();
                return;
            }
            if (event.target.closest("a, button")) {
                closeMenu();
            }
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                closeMenu();
            }
        });

        const breakpoint = window.matchMedia("(min-width: 821px)");
        const handleBreakpoint = () => {
            if (breakpoint.matches) {
                closeMenu();
            }
        };
        if (breakpoint.addEventListener) {
            breakpoint.addEventListener("change", handleBreakpoint);
        } else {
            breakpoint.addListener(handleBreakpoint);
        }

        closeMenu();
    }

    function initUiFeatures() {
        initThemeToggle();
        initDailyRecapQuantControls();
        initHabitProgressControls();
        initDeleteConfirmations();
        initNotificationMenus();
        initScoreDriverHelp();
        initMobileNav();
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
