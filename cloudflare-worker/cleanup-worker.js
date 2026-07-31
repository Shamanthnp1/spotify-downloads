// Cloudflare Worker — R2 Cleanup
// Cron trigger: every 30 minutes  (cron expression: "*/30 * * * *")
//
// Deletes any R2 object whose LastModified date is older than 30 minutes.
// Handles bucket pagination so it covers arbitrarily large buckets.
//
// Binding required in wrangler.toml:
//   [[r2_buckets]]
//   binding = "SPOTIFY_BUCKET"
//   bucket_name = "spotify-downloads"
//
// No external dependencies — pure Workers runtime + R2 bindings.

const STALE_THRESHOLD_MINUTES = 30;

export default {
  /**
   * Cron handler — called by the Workers runtime on schedule.
   * @param {ScheduledEvent} event
   * @param {Object} env         — bound environment (R2 bucket available as env.SPOTIFY_BUCKET)
   * @param {ExecutionContext} ctx
   */
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCleanup(env));
  },

  /**
   * HTTP handler — allows manual trigger via GET /cleanup for debugging.
   * Remove this in production or gate it behind a secret header.
   */
  async fetch(request, env, ctx) {
    if (request.method !== "GET") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const result = await runCleanup(env);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  },
};

/**
 * Core cleanup logic.
 *
 * @param {Object} env
 * @returns {Promise<{ deleted: number, scanned: number, errors: number }>}
 */
async function runCleanup(env) {
  const bucket = env.SPOTIFY_BUCKET;

  if (!bucket) {
    console.error("[cleanup] R2 bucket binding 'SPOTIFY_BUCKET' not found.");
    return { deleted: 0, scanned: 0, errors: 1 };
  }

  const cutoffMs = Date.now() - STALE_THRESHOLD_MINUTES * 60 * 1000;
  const cutoffDate = new Date(cutoffMs);

  let deleted = 0;
  let scanned = 0;
  let errors = 0;
  let cursor = undefined;

  console.log(
    `[cleanup] Starting. Cutoff: ${cutoffDate.toISOString()} ` +
    `(objects older than ${STALE_THRESHOLD_MINUTES} min will be deleted)`
  );

  do {
    /** @type {R2Objects} */
    let listResult;
    try {
      listResult = await bucket.list({
        cursor,
        limit: 1000, // R2 max per page
      });
    } catch (err) {
      console.error(`[cleanup] Failed to list bucket page: ${err.message}`);
      errors += 1;
      break;
    }

    const objects = listResult.objects || [];
    scanned += objects.length;

    const deleteTargets = objects.filter(
      (obj) => obj.uploaded instanceof Date && obj.uploaded < cutoffDate
    );

    for (const obj of deleteTargets) {
      try {
        await bucket.delete(obj.key);
        console.log(
          `[cleanup] Deleted: ${obj.key} ` +
          `(uploaded: ${obj.uploaded.toISOString()})`
        );
        deleted += 1;
      } catch (err) {
        console.error(
          `[cleanup] Failed to delete '${obj.key}': ${err.message}`
        );
        errors += 1;
      }
    }

    cursor = listResult.truncated ? listResult.cursor : undefined;
  } while (cursor !== undefined);

  console.log(
    `[cleanup] Done. Scanned: ${scanned}, Deleted: ${deleted}, Errors: ${errors}`
  );

  return { deleted, scanned, errors };
}
