from PIL import Image

img = Image.open("Icon.png")
img.save("Icon.ico", format='ICO')
print("Converted Icon.png to Icon.ico")
