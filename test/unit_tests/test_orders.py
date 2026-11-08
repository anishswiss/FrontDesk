from business.orders import get_restaurant, get_menu_text
import unittest

class TestOrders(unittest.TestCase):

    def test_get_restaurant(self):
        restaurant = get_restaurant('+19724335803')
        restaurant_name = restaurant.__getitem__("name")
        self.assertEqual(restaurant_name, "Bay Leaf Garden | Kerala & Indian Restaurant | Carrollton, Texas")
    
    def test_get_menu_text(self):
        restaurant = get_restaurant('+19724335803')
        print(restaurant)
        restaurant_id = restaurant.__getitem__("id")
        menu_text = get_menu_text(restaurant_id)
        print(menu_text)
        menu_keyterms = restaurant.__getitem__("speech_hints")
        print("Speech Hints")
        print(menu_keyterms)

        

if __name__ == "main":
    unittest.main()