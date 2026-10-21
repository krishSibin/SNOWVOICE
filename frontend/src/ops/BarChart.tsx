import { useEffect, useRef } from "react";
import { BarController, BarElement, CategoryScale, Chart, LinearScale, Tooltip } from "chart.js";

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip);

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function BarChart({
  labels,
  values,
  horizontal = false,
}: {
  labels: string[];
  values: number[];
  horizontal?: boolean;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (!canvas.current) return;
    const ink = css("--muted");
    const grid = css("--line");
    const chart = new Chart(canvas.current, {
      type: "bar",
      data: { labels, datasets: [{ data: values, backgroundColor: css("--accent"), borderRadius: 4, maxBarThickness: 36 }] },
      options: {
        indexAxis: horizontal ? "y" : "x",
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: ink }, grid: { color: horizontal ? grid : "transparent" }, beginAtZero: true },
          y: { ticks: { color: ink, precision: 0 }, grid: { color: horizontal ? "transparent" : grid }, beginAtZero: true },
        },
      },
    });
    return () => chart.destroy();
  }, [labels, values, horizontal]);

  return <canvas ref={canvas} />;
}
