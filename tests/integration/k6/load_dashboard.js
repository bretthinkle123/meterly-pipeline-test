import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL;
const TARGET_RPS = parseInt(__ENV.TARGET_RPS || '25', 10);
const DURATION = __ENV.DURATION || '60s';
const WARMUP_DURATION = __ENV.WARMUP_DURATION || '5s';

export const options = {
  scenarios: {
    warmup: {
      executor: 'constant-arrival-rate',
      rate: 5,
      timeUnit: '1s',
      duration: WARMUP_DURATION,
      preAllocatedVUs: 10,
      maxVUs: 30,
      exec: 'getUsageSeries',
    },
    ingest: {
      executor: 'constant-arrival-rate',
      rate: TARGET_RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 30,
      maxVUs: 100,
      exec: 'getUsageSeries',
      startTime: WARMUP_DURATION,
    },
  },
};

// No client credential is ever sent (AC9) -- the BFF is app-layer
// unauthenticated for the viewer; the reader key is server-held.
export function getUsageSeries() {
  const response = http.get(
    `${BASE_URL}/dashboard/api/usage-series?customer_id=acme-corp&metric=api_calls&granularity=hour`,
  );
  check(response, { 'status is 200': (r) => r.status === 200 });
}

export function getPage() {
  const response = http.get(`${BASE_URL}/dashboard`);
  check(response, { 'status is 200': (r) => r.status === 200 });
}
