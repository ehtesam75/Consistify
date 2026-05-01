window.ConsistifyCharts = (() => {
    const palette = {
        accent: "#b07a37",
        accentSoft: "rgba(176, 122, 55, 0.2)",
        teal: "#2f6f6d",
        grid: "rgba(28, 31, 36, 0.08)",
    };

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

    function renderRateLine(canvasId, labels, values) {
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
                            callback: (value) => value + "%",
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
                },
            },
        });
    }

    return {
        renderCompletionBars,
        renderRateLine,
    };
})();
