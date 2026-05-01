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

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initThemeToggle);
    } else {
        initThemeToggle();
    }

    return {
        enableHabitDragSort,
    };
})();
