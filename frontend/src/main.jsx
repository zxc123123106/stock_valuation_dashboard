import React from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles/index.css";


const rootElement = document.getElementById("root");
window.__stockDashboardRoot ||= createRoot(rootElement);
window.__stockDashboardRoot.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
