import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'./e2e-m2',workers:1,timeout:120000,use:{baseURL:'http://127.0.0.1:18082',headless:true,trace:'retain-on-failure'},
 webServer:{command:`${process.env.PYTHON || 'python3'} ../tests/serve_m2_fixture.py --port 18082`,url:'http://127.0.0.1:18082/api/health',reuseExistingServer:false,timeout:120000}});
