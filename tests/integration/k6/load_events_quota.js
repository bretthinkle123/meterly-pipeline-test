import http from 'k6/http';
import { check } from 'k6';

// Quota-active variant of load_events.js (AC20): drives the identical
// sustained constant-arrival-rate ingest scenario, but against a caller
// whose (customer, metric) buckets each carry a pre-seeded, deliberately
// high quota (set directly in Postgres by the pytest fixture before this
// script runs) -- so the quota-check read-and-decide executes on every
// request (not the unlimited zero-lookup-cost path) while never actually
// rejecting, isolating the added latency of the FOR UPDATE lookup itself
// from any rejection-path cost.
const BASE_URL = __ENV.BASE_URL;
const API_KEY = __ENV.API_KEY;
const TARGET_RPS = parseInt(__ENV.TARGET_RPS || '500', 10);
const DURATION = __ENV.DURATION || '15s';
const WARMUP_DURATION = __ENV.WARMUP_DURATION || '5s';
const PREALLOC_VUS = parseInt(__ENV.PREALLOC_VUS || '120', 10);
const MAX_VUS = parseInt(__ENV.MAX_VUS || '600', 10);

export const options = {
  scenarios: {
    warmup: {
      executor: 'constant-arrival-rate',
      rate: 10,
      timeUnit: '1s',
      duration: WARMUP_DURATION,
      preAllocatedVUs: 20,
      maxVUs: 50,
      exec: 'postEvent',
    },
    ingest: {
      executor: 'constant-arrival-rate',
      rate: TARGET_RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PREALLOC_VUS,
      maxVUs: MAX_VUS,
      exec: 'postEvent',
      startTime: WARMUP_DURATION,
    },
  },
  discardResponseBodies: false,
};

let counter = 0;

export function postEvent() {
  counter += 1;
  const randomSuffix = Math.floor(Math.random() * 1e9);
  const uniqueId = `${__VU}-${__ITER}-${counter}-${Date.now()}-${randomSuffix}`;
  // Must match the customer-bucket range the pytest fixture pre-seeds a
  // quota for (cust_0..cust_49) -- otherwise these requests would hit the
  // unlimited (no-quota-row) path instead of exercising the quota check.
  const payload = JSON.stringify({
    customer_id: `cust_${__VU % 50}`,
    metric: 'api_calls',
    quantity: '1',
    idempotency_key: `k6-quota-${uniqueId}`,
  });

  const response = http.post(`${BASE_URL}/v1/events`, payload, {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${API_KEY}`,
    },
  });

  check(response, {
    'status is 201 (never 429 -- quota set high enough to never reject)': (r) => r.status === 201,
  });
}
