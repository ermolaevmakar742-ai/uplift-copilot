import unittest

from language_layer.guard import check, protected_tokens


class GuardTest(unittest.TestCase):
    def ok(self, a, b):
        self.assertTrue(check(a, b).ok, check(a, b).violations)

    def bad(self, a, b):
        self.assertFalse(check(a, b).ok)

    def test_identity_and_whitespace(self):
        self.ok("Depósito mínimo $500 MXN", "Depósito mínimo $500 MXN")
        self.ok("Hola  amigo!!", "Hola amigo!")

    def test_numbers_money_currency(self):
        self.bad("Deposita $500", "Deposita $50")
        self.bad("Gana 1,000 pesos", "Gana 1.000 pesos")
        self.bad("Recibe 20% de bono", "Recibe 2% de bono")
        self.bad("Cuesta 100 USD", "Cuesta 100 MXN")
        self.bad("Por R$ 50", "Por R$ 500")
        self.bad("Depósito de 500", "Depósito de")

    def test_urls_handles_codes(self):
        self.bad("Entra a https://site.com/ref?id=7", "Entra a https://site.com/ref?id=8")
        self.bad("Escríbeme a @valeria_ok", "Escríbeme a @valeria")
        self.bad("Usa el código BONO500", "Usa el código BONO50")
        self.bad("Usa BONO500", "Usa bono500")
        self.bad("Mira www.casa.com", "Mira")
        self.bad("mail a ayuda@casa.com", "mail a soporte@casa.com")

    def test_added_tokens_also_violate(self):
        self.bad("Te cuento", "Te cuento: 500")

    def test_question_shape(self):
        self.bad("¿Te animas?", "Te animas.")
        self.ok("Te animas?", "Te animas ?".replace(" ?", "?"))

    def test_empty_result(self):
        self.bad("Hola", "")

    def test_protected_tokens_multiset(self):
        c = protected_tokens("$5 y $5")
        self.assertEqual(sum(v for (k, _), v in c.items() if k == "money"), 2)


if __name__ == "__main__":
    unittest.main()
