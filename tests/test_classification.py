import unittest

from app import assign_priority, categorize_complaint


class CategorizationTests(unittest.TestCase):
    def test_each_supported_category_and_other(self):
        examples = {
            "Electrical": "The ceiling light is broken",
            "Plumbing": "Water pipe is leaking",
            "Equipment": "Projector is not working",
            "Furniture": "A chair is broken",
            "Network": "Wi-Fi connection is down",
            "Other": "The corridor needs cleaning",
        }
        for expected, description in examples.items():
            with self.subTest(category=expected):
                self.assertEqual(categorize_complaint(description), expected)

    def test_first_category_group_wins_for_overlapping_terms(self):
        self.assertEqual(categorize_complaint("Projector socket is damaged"), "Electrical")

    def test_keyword_must_not_be_an_accidental_substring(self):
        self.assertEqual(categorize_complaint("The highlight is too bright"), "Other")


class PriorityTests(unittest.TestCase):
    def test_high_medium_low_and_default_examples(self):
        examples = {
            "High": "Exposed wire near water leakage",
            "Medium": "The classroom fan is broken",
            "Low": "The chair needs paint",
            "Medium": "The corridor needs cleaning",
        }
        for expected, description in examples.items():
            with self.subTest(priority=expected, description=description):
                self.assertEqual(assign_priority(description), expected)

    def test_high_takes_precedence_over_medium_and_low(self):
        self.assertEqual(assign_priority("Fire near a broken fan and chair"), "High")

    def test_water_and_electrical_hazard_is_high(self):
        self.assertEqual(assign_priority("Water is leaking beside the socket"), "High")

    def test_risk_word_must_not_be_an_accidental_substring(self):
        self.assertEqual(assign_priority("The firewall is unavailable"), "Medium")


if __name__ == "__main__":
    unittest.main()
