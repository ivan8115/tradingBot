"""Regression: alerter.alert() requires (event_type, message). These calls must not raise."""


def test_daily_review_alert_call_does_not_raise():
    from monitoring.alerting import AlertManager
    am = AlertManager.__new__(AlertManager)
    am._slack_enabled = False
    am._email_enabled = False
    am._alert_on = {"all"}
    am.alert("daily_review", "Daily Review [A]: Things look good.")


def test_weekly_review_alert_call_does_not_raise():
    from monitoring.alerting import AlertManager
    am = AlertManager.__new__(AlertManager)
    am._slack_enabled = False
    am._email_enabled = False
    am._alert_on = {"all"}
    am.alert("weekly_review", "Weekly Review [B]: premium=$500.00, win_rate=75%")


def test_thesis_warning_alert_call_does_not_raise():
    from monitoring.alerting import AlertManager
    am = AlertManager.__new__(AlertManager)
    am._slack_enabled = False
    am._email_enabled = False
    am._alert_on = {"all"}
    am.alert("thesis_warning", "[Thesis Warning] AAPL: negative guidance cut")
