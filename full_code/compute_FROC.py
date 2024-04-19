import tensorflow as tf
import numpy as np
from scipy import ndimage
import csv
import os.path
import sys

# Import network definition
import model_v4 as model

# Set some parameters
DATA_DIR = "data" # directory with evaluation data
NUM_THRESHOLDS = 50 # number of thresholds for the FROC curve
ACCEPTANCE_RATIO = 0.1 # percentage of overlap to count a lesion as TP


def post(logits, label, threshold):
	""" Creates segmentation assigning every pixel above the threshold a value 
	of 255, pixels where the label==0 to 0 and anything else to 127. 
	
	Args:
		logits: An array of floats with shape [height, width]. A heatmap of 
			logits representing the probability of mass at each pixel.
		label: An array of integers with shape [height, width]. The original
			label used to segment the background from the rest of the image.
		threshold: A float. The logit threshold used for the segmentation.
	
	Returns:
		An array of integers with shape [height, shape]. The produced 
		segmentation with labels 0 (background), 127 (breast tissue) and 255 
		(breast mass)
		
	Note: Using the label may seem like cheating but the label background was 
	generated by thresholding the original image to zero, so (label == 0) is 
	equivalent to comparing the original mammogram to zero.
	"""
	thresholded = np.ones(logits.shape, dtype='uint8') * 127
	thresholded[logits >= threshold] = 255
	thresholded[label == 0] = 0
	
	return thresholded
	
def compute_FROC(logits, label, num_thresholds, acceptance_ratio):
	""" Computes the number of correctly localized lesions (TPs) and incorrect 
		localizations (FPs) at different thresholds for one image.
	
	A lesion is considered localized if a blob in the segmentation (a contiguos
	cluster of positive predictions) covers at least a percentage of the lesion
	area (defined by the acceptance_ratio).
	
	Only images with no lesions are used to calculate FPs. In these images, a FP
	is any blob in the segmentation. For less strict thresholds, the number of 
	FPs is forced to be non-decreasing.

	Args:
		logits: An array of floats with shape [height, width]. A heatmap of 
			logits representing the probability of mass at each pixel.
		label: An array of integers with shape [height, width]. The expected 
			label.
		num_thresholds: An integer. The number of thresholds used to calculate 
			the FROC curve.
		acceptance_ratio: A float. The percentage of overlap needed to classify
			a lesion as correctly localized.
			
	Returns:
		FPs: Array of integers with shape [num_thresholds]. FPs at each 
			threshold.
		TPs: Array of integers with shape [num_thresholds]. TPs at each 
			threshold.
		num_lesions: An integer. Number of lesions in the image.
	"""	
	# Get thresholds
	probs = np.linspace(0.9999, 0.0001, num_thresholds) # uniformly distributed
	thresholds = np.log(probs) - np.log(1 - probs) #prob2logit
	
	# Initialize containers
	TPs = np.zeros(num_thresholds)
	FPs = np.zeros(num_thresholds)
	num_lesions = 0
	
	# Over each image
	for threshold in range(num_thresholds):
		# Create segmentation
		segmentation = post(logits, label, thresholds[threshold])
		
		if label.max() == 255: # if the image had lesions
			# Find lesions
			structure_mask = [[1,1,1], [1,1,1], [1,1,1]]
			lesions, num_lesions = ndimage.label(label == 255, structure_mask)
			
			# Add 1 to TP if lesion correctly identified
			for lesion_id in range(1, num_lesions + 1):
				lesion_area = (lesions == lesion_id).sum()
				overlap_area = np.logical_and(lesions == lesion_id,
											  segmentation == 255).sum()
				if (overlap_area / lesion_area) >= ACCEPTANCE_RATIO:
					TPs[threshold] += 1

		else: # no lesions
			# Find all FPs
			structure_mask = [[1,1,1], [1,1,1], [1,1,1]]
			_, num_FPs = ndimage.label(segmentation == 255, structure_mask)
			
			# Assign them to the current threshold
			FPs[threshold] += num_FPs
			
			# Force FPs to be non-decreasing
			if FPs[threshold] < FPs[threshold - 1]: 
				FPs[threshold] = FPs[threshold -1]

	return FPs, TPs, num_lesions
	
def main(data_dir=DATA_DIR, num_thresholds=NUM_THRESHOLDS, 
		 acceptance_ratio=ACCEPTANCE_RATIO):
	""" Reads evaluation data, loads model and computes the FROC curve."""
	# Model directory and path to the csv passed as arguments
	model_dir = sys.argv[1]
	csv_path = sys.argv[2]

	# Read csv file
	with open(csv_path) as f:
		lines = f.read().splitlines()
	csv_reader = csv.reader(lines)
	
	# Image as placeholder
	image = tf.placeholder(tf.float32, name='image')
	whitened = tf.image.per_image_whitening(tf.expand_dims(image, 2))
	
	# Define the model
	prediction = model.forward(whitened, drop=tf.constant(False))
		
	# Get a saver to load the model
	saver = tf.train.Saver()

	# Use CPU-only. To enable GPU, delete this and call with tf.Session() as ...
	config = tf.ConfigProto(device_count={'GPU':0})
	
	# Initialize some variables
	FPs = np.zeros(NUM_THRESHOLDS) # accumulates FPs over images
	TPs = np.zeros(NUM_THRESHOLDS) # acumulates TPs over images
	num_normal_images = 0 # images with no lesions
	num_lesions = 0
	 
	# Launch graph
	with tf.Session(config=config) as sess:
		# Restore variables
		checkpoint_path = tf.train.latest_checkpoint(model_dir)
		print("Restoring model from:", checkpoint_path)
		saver.restore(sess, checkpoint_path)
		
		# For every example
		for row in csv_reader:
			# Read paths
			image_path = os.path.join(data_dir, row[0])
			label_path = os.path.join(data_dir, row[1])

			# Read image and label
			im = ndimage.imread(image_path)
			label = ndimage.imread(label_path)
		
			# Get prediction
			logits = prediction.eval({image: im})
			
			# Compute TP and FP over all thresholds
			im_FPs, im_TPs, im_lesions = compute_FROC(logits, label, 
										       num_thresholds, acceptance_ratio)
			
			# Accumulate output
			FPs += im_FPs
			TPs += im_TPs
			num_lesions += im_lesions
			if im_lesions == 0:
				num_normal_images += 1		
					
	# Compute final metrics
	sensitivity = TPs/num_lesions
	FP_per_image = FPs/num_normal_images
	sensitivity_at_1_FP = np.interp(1, FP_per_image, sensitivity)
			
	# Report metrics
	print('Sensitivity: ')
	print(sensitivity)
	print('FP/image: ')
	print(FP_per_image)
	print('Sensitivity at 1 FP: ', sensitivity_at_1_FP)
	
	# Write results to file
	with open(os.path.join(model_dir, 'FROC'), 'w') as f:
		f.write('Model: ' + checkpoint_path + '\n')
		f.write('csv: ' + csv_path + '\n')
		f.write('Sensitivity: \n' + str(sensitivity) + '\n')
		f.write('FP/image: \n' + str(FP_per_image) + '\n')
		f.write('Sensitivity at 1 FP: ' + str(sensitivity_at_1_FP))	
					
	return sensitivity, FP_per_image, sensitivity_at_1_FP
	
if __name__ == "__main__":
	main()
