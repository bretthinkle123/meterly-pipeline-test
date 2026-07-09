import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL;
const API_KEY = __ENV.API_KEY;
const TARGET_RPS = parseInt(__ENV.TARGET_RPS || '500', 10);
const DURATION = __ENV.DURATION || '15s';
const WARMUP_DURATION = __ENV.WARMUP_DURATION || '5s';
const PREALLOC_VUS = parseInt(__ENV.PREALLOC_VUS || '120', 10);
const MAX_VUS = parseInt(__ENV.MAX_VUS || '600', 10);

// A warm-up scenario at a low, fixed rate runs first (populates the
// Argon2id verification cache + DB connection pools) before the measured
// "ingest" scenario starts (constant-arrival-rate at the real target rate).
// Only requests tagged scenario=ingest are used for the recorded p95/
// throughput -- cold-start latency in the warm-up is deliberately excluded,
// matching how a real load-test rig ramps up before recording.
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
  const payload = JSON.stringify({
    customer_id: `cust_${__VU % 50}`,
    metric: 'api_calls',
    quantity: '1',
    idempotency_key: `k6-${uniqueId}`,
  });

  const response = http.post(`${BASE_URL}/v1/events`, payload, {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${API_KEY}`,
    },
  });

  check(response, {
    'status is 200 or 201': (r) => r.status === 200 || r.status === 201,
  });
}
