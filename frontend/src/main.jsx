import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";

import App from "./App";
import { queryClient } from "./queryClient";
import "./styles/index.css";


const rootElement = document.getElementById("root");
window.__stockDashboardRoot ||= createRoot(rootElement);
window.__stockDashboardRoot.render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
