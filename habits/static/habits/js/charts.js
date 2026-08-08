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

    function getMobileTargetTickCount(canvas, labelsLength, chartOptions = {}) {
        if (!isMobileViewport()) {
            return labelsLength;
        }

        const fallbackWidth = window.innerWidth || 0;
        const chartWidth = canvas && canvas.clientWidth ? canvas.clientWidth : fallbackWidth;
        const labelSpacing = Number.isFinite(chartOptions.mobileLabelSpacing) && chartOptions.mobileLabelSpacing > 0
            ? chartOptions.mobileLabelSpacing
            : chartOptions.formatMonthLabel
                ? 96
                : 120;
        const widthBasedTickCount = Math.max(3, Math.floor(chartWidth / labelSpacing));

        return Math.min(labelsLength, widthBasedTickCount);
    }

    function getVisibleTickIndices(labelsLength, targetTickCount) {
        if (labelsLength <= targetTickCount) {
            return null;
        }

        const visibleIndices = new Set([0, labelsLength - 1]);
        const middleTickCount = Math.max(0, targetTickCount - 2);

        for (let slot = 1; slot <= middleTickCount; slot += 1) {
            const index = Math.round((slot * (labelsLength - 1)) / (middleTickCount + 1));
            visibleIndices.add(index);
        }

        return visibleIndices;
    }

    function createResponsiveTickCallback(canvas, labels, chartOptions = {}) {
        return (value, index) => {
            const targetTickCount = getMobileTargetTickCount(canvas, labels.length, chartOptions);
            const visibleTickIndices = getVisibleTickIndices(labels.length, targetTickCount);
            const label = labels[index];

            if (visibleTickIndices && !visibleTickIndices.has(index)) {
                return "";
            }

            return getDisplayedLabel(label, {
                trimRangeStart: chartOptions.trimRangeStart === true,
                formatMonthLabel: Boolean(chartOptions.formatMonthLabel),
            });
        };
    }

    function buildGradient(ctx, height) {
        const gradient = ctx.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, "rgba(176, 122, 55, 0.4)");
        gradient.addColorStop(1, "rgba(176, 122, 55, 0.05)");
        return gradient;
    }

    function renderCompletionBars(canvasId, labels, values, chartOptions = {}) {
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
                        ticks: {
                            autoSkip: false,
                            callback: createResponsiveTickCallback(canvas, labels, {
                                ...chartOptions,
                                trimRangeStart: false,
                            }),
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
                            autoSkip: false,
                            callback: createResponsiveTickCallback(canvas, labels, {
                                ...chartOptions,
                                trimRangeStart: false,
                            }),
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

    function renderCompletionLine(canvasId, labels, values, chartOptions = {}) {
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
                        ticks: {
                            autoSkip: false,
                            callback: createResponsiveTickCallback(canvas, labels, {
                                ...chartOptions,
                                trimRangeStart: false,
                            }),
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

        // Both series share a single axis when they already use the same scale
        // (e.g. a completion percentage and a 0-100 consistency score). A second
        // axis in that case is redundant and makes comparable lines look as if
        // they were measured differently.
        const shareAxis = secondaryAxisConfig.shareAxis === true;
        const secondaryAxisId = shareAxis ? "y" : "y1";

        const scales = {
            y: {
                beginAtZero: true,
                max: 100,
                ticks: {
                    stepSize: shareAxis ? secondaryAxisConfig.stepSize || 20 : 20,
                    callback: shareAxis
                        ? secondaryAxisConfig.tickFormatter || ((value) => value)
                        : (value) => value + "%",
                },
                grid: {
                    color: palette.grid,
                },
            },
            x: {
                ticks: {
                    autoSkip: false,
                    callback: createResponsiveTickCallback(canvas, labels, {
                        ...chartOptions,
                        trimRangeStart: chartOptions.trimRangeStart !== false,
                    }),
                    maxRotation: 0,
                    minRotation: 0,
                },
                grid: {
                    display: false,
                },
            },
        };

        if (!shareAxis) {
            scales.y1 = {
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
            };
        }

        new window.Chart(ctx, {
            type: "line",
            data: {
                labels,
                datasets: [
                    {
                        label: primaryLabel,
                        data: primaryValues,
                        valueSuffix: "%",
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
                        yAxisID: secondaryAxisId,
                        valueSuffix: secondaryAxisConfig.valueSuffix || "",
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales,
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            usePointStyle: true,
                        },
                    },
                    tooltip: {
                        callbacks: {
                            label: (context) => {
                                const value = context.parsed.y;
                                if (value == null) {
                                    return `${context.dataset.label}: no data`;
                                }
                                const suffix = context.dataset.valueSuffix || "";
                                return `${context.dataset.label}: ${value}${suffix}`;
                            },
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
