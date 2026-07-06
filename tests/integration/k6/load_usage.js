import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL;
const API_KEY = __ENV.API_KEY;
const TARGET_RPS = parseInt(__ENV.TARGET_RPS || '100', 10);
const DURATION = __ENV.DURATION || '10s';
const WARMUP_DURATION = __ENV.WARMUP_DURATION || '3s';

export const options = {
  scenarios: {
    warmup: {
      executor: 'constant-arrival-rate',
      rate: 10,
      timeUnit: '1s',
      duration: WARMUP_DURATION,
      preAllocatedVUs: 10,
      maxVUs: 30,
      exec: 'getUsage',
    },
    ingest: {
      executor: 'constant-arrival-rate',
      rate: TARGET_RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 30,
      maxVUs: 150,
      exec: 'getUsage',
      startTime: WARMUP_DURATION,
    },
  },
};

export function getUsage() {
  const now = new Date().toISOString();
  const custId = `cust_${__VU % 50}`;
  const response = http.get(
    `${BASE_URL}/v1/usage?customer_id=${custId}&metric=api_calls&window=${encodeURIComponent(now)}`,
    { headers: { Authorization: `Bearer ${API_KEY}` } },
  );
  check(response, { 'status is 200': (r) => r.status === 200 });
}
