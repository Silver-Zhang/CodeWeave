import { defineConfig } from '@playwright/test';
export default defineConfig({ testDir:'./e2e', workers:1, use:{baseURL:'http://127.0.0.1:18080',headless:true,trace:'retain-on-failure'},
  webServer:{command:`${process.env.PYTHON || 'python3'} ../tests/serve_m1_fixture.py --port 18080`,url:'http://127.0.0.1:18080/api/health',reuseExistingServer:false,timeout:120000},
});
