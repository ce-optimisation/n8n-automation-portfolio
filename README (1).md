# Enterprise Automation & Workflow Engineering Portfolio

This repository contains production-ready, modular **n8n workflow blueprints** built to eliminate operational inefficiencies, accelerate lead velocity, and prevent business revenue leakage. Every workflow utilizes an isolated global configuration pattern and standardized data mapping layers for enterprise scalability.

---

## 📂 Portfolio Structure

*   [README.md](README.md) - Portfolio overview and architecture standards.
*   [lead-management-revops.md](lead-management-revops.md) - Lead Intelligence, Quoting, and Re-engagement automations.
*   [client-retention-accounts.md](client-retention-accounts.md) - Contract Renewals and Churn Save systems.
*   [internal-governance-logistics.md](internal-governance-logistics.md) - Approvals, Escalations, No-shows, and Inventory triggers.

---

## 🛠️ Global Deployment Instructions

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/yourusername/n8n-automation-portfolio.git
    ```
2.  **Import to n8n:**
    *   Open your n8n instance.
    *   Create a new workflow canvas.
    *   Click the top-right menu and choose **Import from File**. Select your desired workflow `.json` file.
3.  **Environment Configuration:**
    *   Every workflow features an isolated `Config` node near the trigger. 
    *   Double-click the `Config` node to replace placeholders (such as `<<EDIT_ME>>` URLs and email parameters) with your real credentials and system API endpoints.