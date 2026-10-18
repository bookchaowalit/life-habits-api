import { defineRailway, project, service } from "railway/iac";

// life-habits-api: the habits cell. Variables stay unmanaged (secrets live in Railway only).
// habits-api sleeps when idle (Railway Serverless) and wakes on the next request;
// habits-publisher is a cron sweep of the outbox that exits after each run;
// habits-autogrant is the weekly cron that logs unlogged scheduled days through habits-api.
// Every service deploys from GitHub main after CI passes (Railway Wait for CI, 2026-10-18).
// Declared here so `railway config apply` keeps the connection instead of removing it.
const github = {
  type: "github" as const,
  repo: "bookchaowalit/life-habits-api",
  branch: "main",
  checkSuites: true,
};

export default defineRailway(() => {
  const api = service("habits-api", {
    source: github,
    healthcheck: "/health",
    healthcheckTimeout: 60,
    deploy: { sleepApplication: true },
  });
  const publisher = service("habits-publisher", {
    source: github,
    startCommand: "habits-publish",
    deploy: { cronSchedule: "*/15 * * * *", restartPolicyType: "NEVER" },
  });

  // 15 17 * * 6 UTC = Sunday 00:15 Asia/Bangkok, the schedule of the retired local
  // "weekly_habit_autogranted" job (15 0 * * 0).
  const autogrant = service("habits-autogrant", {
    source: github,
    startCommand: "habits-autogrant",
    deploy: { cronSchedule: "15 17 * * 6", restartPolicyType: "NEVER" },
  });

  return project("life-habits-api", {
    variables: { managed: false },
    resources: [api, publisher, autogrant],
  });
});
