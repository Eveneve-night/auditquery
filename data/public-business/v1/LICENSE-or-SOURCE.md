# Public synthetic development assets

Original project-authored synthetic data; redistribution permitted under MIT. No real business/customer records or credentials. All samples are exposed prompt_dev, never hidden validation. Orders reuse the existing 39-table order_to_cash DDL without altering it. Maintenance is a new domain and does not rename equipment_loan.

DDL and seed JSON are runtime inputs. Scoring/questions live separately under tests/fixtures; the builder never loads them. Human configuration effort is unknown, not zero. Local read-only fixture checks do not prove production data-source onboarding or permission to execute JOIN.
