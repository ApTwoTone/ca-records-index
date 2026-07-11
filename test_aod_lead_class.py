import unittest

import lead_class
import select_lead_docs


class AodLeadClassTest(unittest.TestCase):
    def test_exact_estate_transfer_types(self):
        expected = {
            "AFFIDAVIT SUCCESSOR TRUSTEE": "affidavit_successor_trustee",
            "AFFIDAVIT SUCCESSION INTEREST": "affidavit_succession_interest",
            "REVOCABLE TRANSFER DEATH DEED": "revocable_transfer_death_deed",
            "REVOCABLE TRANSFER ON DEATH DEED": "revocable_transfer_death_deed",
        }
        for raw, classification in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(lead_class.lead_class(raw), classification)
                self.assertIn(classification, select_lead_docs.PROPERTY_BEARING)

    def test_nearby_non_targets_do_not_leak_into_aod(self):
        aod_classes = {
            "affidavit_death",
            "affidavit_death_unspecified",
            "affidavit_successor_trustee",
            "affidavit_succession_interest",
            "revocable_transfer_death_deed",
        }
        raw_types = [
            "CANCELLATION NOTICE OF DEFAULT",
            "AFFIDAVIT OF IDENTITY",
            "SUCCESSOR TRUSTEE",
            "TRUST",
            "REVOCABLE TRUST DEED",
        ]
        for raw in raw_types:
            with self.subTest(raw=raw):
                self.assertNotIn(lead_class.lead_class(raw), aod_classes)


if __name__ == "__main__":
    unittest.main()
