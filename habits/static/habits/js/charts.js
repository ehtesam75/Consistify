window.ConsistifyCharts = (() => {
    const MOBILE_BREAKPOINT = "(max-width: 820px)";
    const palette = {
        accent: "#b07a37",
        accentSoft: "rgba(176, 122, 55, 0.2)",
        teal: "#2f6f6d",
        grid: "rgba(28, 31, 36, 0.08)",
    };

    function isMobileViewport() {
        return window.matchMedia && window.matchMedia(MOBILE_BREAKPOINT).matches;
    }

    function formatMonthLabel(label) {
        if (typeof label !== "string") {
            return label;
        }

        const directDate = new Date(label);
        if (!Number.isNaN(directDate.getTime())) {
            return directDate.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
        }

        const monthYearMatch = label.match(/^([A-Za-z]{3,9})\s+(\d{2,4})$/);
        if (monthYearMatch) {
            const normalized = new Date(`${monthYearMatch[1]} 1, ${monthYearMatch[2]}`);
            if (!Number.isNaN(normalized.getTime())) {
                return normalized.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
            }
        }

        const isoMonthMatch = label.match(/^(\d{4})-(\d{2})(?:-\d{2})?$/);
        if (isoMonthMatch) {
            const year = Number(isoMonthMatch[1]);
            const monthIndex = Number(isoMonthMatch[2]) - 1;
            const isoDate = new Date(year, monthIndex, 1);
            if (!Number.isNaN(isoDate.getTime())) {
                return isoDate.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
            }
        }

        return label;
    }

    function getDisplayedLabel(label, options = {}) {
        let output = label;
        if (typeof output === "string" && options.trimRangeStart && output.includes(" - ")) {
            output = output.split(" - ")[0];
        }
        if (options.formatMonthLabel) {
            output = formatMonthLabel(output);
        }
        return output;
    }

    function shouldDisplayMobileTick(index, labelsLength, targetTickCount) {
        if (labelsLength <= targetTickCount) {
            return true;
        }
        const interval = Math.max(1, Math.ceil(labelsLength / targetTickCount));
        return index % interval === 0 || index === labelsLength - 1;
    }

    function buildGradient(ctx, height) {
        const gradient = ctx.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, "rgba(176, 122, 55, 0.4)");
        gradient.addColorStop(1, "rgba(176, 122, 55, 0.05)");
        return gradient;
    }

    function renderCompletionBars(canvasId, labels, values) {
        const canvas = document.getElementById(canvasId);
        if (!canvas || !window.Chart) {
            return;
        }

        const ctx = canvas.getContext("2d");
        const gradient = buildGradient(ctx, canvas.height || 240);

        new window.Chart(ctx, {
            type: "bar",
            data: {
                labels,
                datasets: [
                    {
                        label: "Completed",
                        data: values,
                        backgroundColor: gradient,
                        borderColor: palette.accent,
                        borderWidth: 2,
                        borderRadius: 10,
                        barThickness: 16,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 1,
                        ticks: {
                            stepSize: 1,
                            callback: (value) => (value ? "Done" : "Missed"),
                        },
                        grid: {
                            color: palette.grid,
                        },
                    },
                    x: {
                        grid: {
                            display: false,
                        },
                    },
                },
                plugins: {
                    legend: {
                        display: false,
                    },
                    tooltip: {
                        callbacks: {
                            label: (context) => (context.raw ? "Completed" : "Missed"),
                        },
                    },
                },
            },
        });
    }

    function renderRateLine(canvasId, labels, values, chartOptions = {}) {
        const canvas = document.getElementById(canvasId);
        if (!canvas || !window.Chart) {
            return;
        }

        const ctx = canvas.getContext("2d");
        const gradient = buildGradient(ctx, canvas.height || 240);

        new window.Chart(ctx, {
            type: "line",
            data: {
                labels,
                datasets: [
                    {
                        label: "Completion rate",
                        data: values,
                        backgroundColor: gradient,
                        borderColor: palette.teal,
                        borderWidth: 2,
                        fill: true,
                        tension: 0.35,
                        pointRadius: 3,
                        pointBackgroundColor: palette.teal,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100,
                        ticks: {
                            stepSize: 20,
                            callback: (value) => value + "%",
                        },
                        grid: {
                            color: palette.grid,
                        },
                    },
                    x: {
                        ticks: {
                            callback: (value, index) => {
                                const label = labels[index];
                                const mobileTargetTicks = chartOptions.mobileTickCount;
                                const shouldLimitTicks = isMobileViewport() && Number.isInteger(mobileTargetTicks) && mobileTargetTicks > 0;
                                const showTick = !shouldLimitTicks || shouldDisplayMobileTick(index, labels.length, mobileTargetTicks);
                                if (!showTick) {
                                    return "";
                                }
                                return getDisplayedLabel(label, {
                                    formatMonthLabel: Boolean(chartOptions.formatMonthLabel),
                                });
                            },
                            maxRotation: 0,
                            minRotation: 0,
                        },
                        grid: {
                            display: false,
                        },
                    },
                },
                plugins: {
                    legend: {
                        display: false,
                    },
                },
            },
        });
    }

    function renderCompletionLine(canvasId, labels, values) {
        const canvas = document.getElementById(canvasId);
        if (!canvas || !window.Chart) {
            return;
        }

        const ctx = canvas.getContext("2d");
        const gradient = buildGradient(ctx, canvas.height || 240);

        new window.Chart(ctx, {
            type: "line",
            data: {
                labels,
                datasets: [
                    {
                        label: "Completion status",
                        data: values,
                        backgroundColor: gradient,
                        borderColor: palette.teal,
                        borderWidth: 2,
                        fill: true,
                        tension: 0.35,
                        pointRadius: 3,
                        pointBackgroundColor: palette.teal,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        min: 0,
                        max: 1,
                        ticks: {
                            stepSize: 1,
                            callback: (value) => (value ? "Done" : "Missed"),
                        },
                        grid: {
                            color: palette.grid,
                        },
                    },
                    x: {
                        grid: {
                            display: false,
                        },
                    },
                },
                plugins: {
                    legend: {
                        display: false,
                    },
                    tooltip: {
                        callbacks: {
                            label: (context) => (context.raw ? "Completed" : "Missed"),
                        },
                    },
                },
            },
        });
    }

    function renderTrendLine(
        canvasId,
        labels,
        primaryValues,
        secondaryValues,
        primaryLabel,
        secondaryLabel,
        secondaryAxisConfig = {},
        chartOptions = {}
    ) {
        const canvas = document.getElementById(canvasId);
        if (!canvas || !window.Chart) {
            return;
        }

        const ctx = canvas.getContext("2d");
        const gradient = buildGradient(ctx, canvas.height || 240);

        new window.Chart(ctx, {
            type: "line",
            data: {
                labels,
                datasets: [
                    {
                        label: primaryLabel,
                        data: primaryValues,
                        borderColor: palette.teal,
                        backgroundColor: gradient,
                        borderWidth: 2,
                        fill: true,
                        tension: 0.3,
                        pointRadius: 3,
                        pointBackgroundColor: palette.teal,
                        yAxisID: "y",
                    },
                    {
                        label: secondaryLabel,
                        data: secondaryValues,
                        borderColor: palette.accent,
                        backgroundColor: "rgba(176, 122, 55, 0.08)",
                        borderWidth: 2,
                        fill: false,
                        tension: 0.3,
                        pointRadius: 3,
                        pointBackgroundColor: palette.accent,
                        yAxisID: "y1",
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100,
                        ticks: {
                            stepSize: 20,
                            callback: (value) => value + "%",
                        },
                        grid: {
                            color: palette.grid,
                        },
                    },
                    y1: {
                        beginAtZero: true,
                        min: secondaryAxisConfig.min,
                        max: secondaryAxisConfig.max,
                        position: "right",
                        ticks: {
                            stepSize: secondaryAxisConfig.stepSize,
                            callback: secondaryAxisConfig.tickFormatter,
                        },
                        grid: {
                            drawOnChartArea: false,
                        },
                    },
                    x: {
                        ticks: {
                            callback: (value, index) => {
                                const label = labels[index];
                                const mobileTargetTicks = chartOptions.mobileTickCount;
                                const shouldLimitTicks = isMobileViewport() && Number.isInteger(mobileTargetTicks) && mobileTargetTicks > 0;
                                const showTick = !shouldLimitTicks || shouldDisplayMobileTick(index, labels.length, mobileTargetTicks);
                                if (!showTick) {
                                    return "";
                                }
                                return getDisplayedLabel(label, {
                                    trimRangeStart: chartOptions.trimRangeStart !== false,
                                    formatMonthLabel: Boolean(chartOptions.formatMonthLabel),
                                });
                            },
                            maxRotation: 0,
                            minRotation: 0,
                        },
                        grid: {
                            display: false,
                        },
                    },
                },
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            usePointStyle: true,
                        },
                    },
                },
            },
        });
    }

    return {
        renderCompletionBars,
        renderCompletionLine,
        renderRateLine,
        renderTrendLine,
    };
})();
